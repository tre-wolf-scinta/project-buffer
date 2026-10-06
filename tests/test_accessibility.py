"""Structural accessibility checks on rendered pages.

These catch regressions in landmarks, headings, labels and accessible names.
They do not replace the manual screen-reader checks in ACCESSIBILITY.md.
"""

from __future__ import annotations

import re
from collections import defaultdict

import pytest
from bs4 import BeautifulSoup, Tag
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from project_buffer.domain.analysis import DraftReplyResult
from project_buffer.domain.enums import Direction, Urgency
from project_buffer.infrastructure.db.models import Draft, Message, User
from project_buffer.infrastructure.llm.base import LLMTransientError
from project_buffer.services.container import Services
from project_buffer.services.drafts import body_digest
from project_buffer.worker import drain
from tests.conftest import (
    STRANGER,
    ScriptedLLM,
    analysis,
    inbound_params,
    post_inbound,
    sign_in,
)

LABELLED_CONTROLS = ("input", "select", "textarea")


def _name(element: Tag) -> str:
    """Approximate accessible name: aria-label, else text content including hidden spans."""
    if element.get("aria-label"):
        return str(element["aria-label"]).strip()
    return re.sub(r"\s+", " ", element.get_text()).strip()


def audit(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    problems: list[str] = []

    if not (soup.html and soup.html.get("lang")):
        problems.append("html element has no lang attribute")
    title = soup.title.get_text(strip=True) if soup.title else ""
    if not title or title == "- Buffer":
        problems.append("page has no meaningful title")
    if not soup.find("meta", {"name": "viewport"}):
        problems.append("no viewport meta tag")

    if len(soup.find_all("main")) != 1:
        problems.append("page must have exactly one main landmark")
    if len(soup.find_all("h1")) != 1:
        problems.append(f"page must have exactly one h1, found {len(soup.find_all('h1'))}")
    skip = soup.find("a", class_="skip-link")
    if not skip or skip.get("href") != "#main" or not soup.find(id="main"):
        problems.append("skip link to main content is missing")

    previous = 0
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        level = int(heading.name[1])
        if not _name(heading):
            problems.append(f"empty {heading.name}")
        if previous and level > previous + 1:
            problems.append(f"heading level jumps from h{previous} to h{level}: {_name(heading)!r}")
        previous = level

    navs = soup.find_all("nav")
    if len(navs) > 1 and any(not nav.get("aria-label") for nav in navs):
        problems.append("multiple nav landmarks need distinct aria-labels")
    labels = [nav.get("aria-label") for nav in navs]
    if len(labels) != len(set(labels)):
        problems.append("nav landmarks share a label")

    ids = [element["id"] for element in soup.find_all(id=True)]
    duplicates = {value for value in ids if ids.count(value) > 1}
    if duplicates:
        problems.append(f"duplicate ids: {sorted(duplicates)}")
    for attribute in ("aria-labelledby", "aria-describedby"):
        for element in soup.find_all(attrs={attribute: True}):
            for reference in str(element[attribute]).split():
                if reference not in ids:
                    problems.append(f"{attribute} points at missing id {reference!r}")
    for label in soup.find_all("label"):
        if label.get("for") not in ids:
            problems.append(f"label {_name(label)!r} is not attached to a control")

    for control in soup.find_all(LABELLED_CONTROLS):
        if control.get("type") == "hidden":
            continue
        control_id = control.get("id")
        if not (control_id and soup.find("label", {"for": control_id})) and not control.get(
            "aria-label"
        ):
            problems.append(f"form control without a label: {control.get('name')!r}")

    for button in soup.find_all("button"):
        if not _name(button):
            problems.append("button without an accessible name")
    for image in soup.find_all("img"):
        if image.get("alt") is None:
            problems.append("img without alt")

    # Links and buttons that do different things must not share a name.
    destinations: dict[str, set[str]] = defaultdict(set)
    for link in soup.find_all("a"):
        name = _name(link)
        if not name:
            problems.append(f"link without an accessible name: {link.get('href')}")
        destinations[name].add(str(link.get("href")))
    for button in soup.find_all("button"):
        form = button.find_parent("form")
        action = form.get("action", "") if form else ""
        target = f"{action}#{button.get('name', '')}={button.get('value', '')}"
        if form:
            extra = form.find("input", {"name": "handled"})
            target += f"#{extra['value']}" if extra else ""
        destinations[_name(button)].add(target)
    for name, targets in destinations.items():
        if len(targets) > 1:
            problems.append(f"ambiguous name {name!r} is used for {len(targets)} different actions")

    for element in soup.find_all(attrs={"tabindex": True}):
        if int(element["tabindex"]) > 0:
            problems.append("positive tabindex disrupts focus order")
    for element in soup.find_all(True):
        if any(attribute.startswith("on") for attribute in element.attrs):
            problems.append(f"inline event handler on {element.name}")
        if element.name in ("div", "span") and element.get("onclick"):
            problems.append("click handler on a non-interactive element")
    for element in soup.find_all(attrs={"role": "status"}):
        if element.name not in ("p", "div"):
            problems.append("status region should be a simple container")
    return problems


@pytest.fixture
def populated(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> dict[str, str]:
    """A conversation with every message state, and the URL of each page to audit."""
    llm.analyses.extend(
        [
            analysis(
                short_summary="Jordan asks for school transportation on Wednesday.",
                topic="School transportation",
                requests=["Transportation to and from school on Wednesday."],
                factual_information=["Her father is unavailable on Wednesday."],
                sender_statements=["Jordan reports she has no vehicle."],
                sender_positions=["Jordan does not want the children on the bus."],
                allegations_relevant_to_parenting_or_legal_matters=[
                    "Jordan alleges the schedule was not followed."
                ],
                dates_and_times=[{"what": "School run", "when": "Wednesday"}],
                response_needed=True,
                omitted_content_present=True,
                omitted_content_categories=["insults"],
                school_issue=True,
            ),
            analysis(
                short_summary="Sam is in the emergency room with a possible broken arm.",
                topic="Sam injured at school",
                urgency=Urgency.EMERGENCY,
                safety_issue=True,
                response_needed=True,
            ),
            analysis(short_summary="Riley's recital is on Friday.", topic="Recital"),
            LLMTransientError("x"),
        ]
    )
    for body in ("Take them Wednesday you jerk", "Sam is in the ER", "Recital Friday"):
        post_inbound(auth_client, inbound_params(body))
        drain(services)
    post_inbound(auth_client, inbound_params("still being filtered"))
    post_inbound(auth_client, inbound_params("who is this", sender=STRANGER))

    inbound = db.scalars(
        select(Message).where(Message.direction == Direction.INBOUND).order_by(Message.created_at)
    ).all()
    first, second, third, pending, quarantined = inbound

    llm.drafts.extend(
        [
            DraftReplyResult(
                message_text="We can't do Wednesday.", notes_for_owner=["Kept short."]
            ),
            DraftReplyResult(message_text="Thanks, noted.", notes_for_owner=[]),
        ]
    )
    token = auth_client.csrf
    auth_client.post(f"/messages/{first.id}/reply", data={"instruction": "no", "csrf_token": token})
    auth_client.post(f"/messages/{third.id}/reply", data={"instruction": "ok", "csrf_token": token})
    open_draft, sent_draft = db.scalars(select(Draft).order_by(Draft.created_at)).all()
    auth_client.post(
        f"/drafts/{sent_draft.id}/send",
        data={
            "csrf_token": token,
            "approve": "yes",
            "reviewed_digest": body_digest(sent_draft.body),
        },
    )
    outbound = db.scalars(select(Message).where(Message.direction == Direction.OUTBOUND)).one()

    return {
        "inbox": "/inbox",
        "inbox all": "/inbox?filter=all",
        "inbox urgent": "/inbox?filter=urgent",
        "inbox handled": "/inbox?filter=handled",
        "inbox quarantined": "/inbox?filter=quarantined",
        "inbox with notice": "/inbox?notice=handled",
        "timeline": "/timeline",
        "message detail": f"/messages/{first.id}",
        "message pending": f"/messages/{pending.id}",
        "message quarantined": f"/messages/{quarantined.id}",
        "message replied": f"/messages/{third.id}",
        "sent message": f"/messages/{outbound.id}",
        "original warning": f"/messages/{first.id}/original",
        "reply": f"/messages/{second.id}/reply",
        "compose": "/compose",
        "draft": f"/drafts/{open_draft.id}",
        "draft review": f"/drafts/{open_draft.id}/review",
        "drafts": "/drafts",
        "search": "/search",
        "export": "/export",
        "account": "/account",
    }


PAGE_NAMES = [
    "inbox",
    "inbox all",
    "inbox urgent",
    "inbox handled",
    "inbox quarantined",
    "inbox with notice",
    "timeline",
    "message detail",
    "message pending",
    "message quarantined",
    "message replied",
    "sent message",
    "original warning",
    "reply",
    "compose",
    "draft",
    "draft review",
    "drafts",
    "search",
    "export",
    "account",
]


@pytest.mark.parametrize("page", PAGE_NAMES)
def test_authenticated_pages_pass_structural_audit(
    auth_client: TestClient, populated: dict[str, str], page: str
) -> None:
    response = auth_client.get(populated[page])
    assert response.status_code == 200, f"{page} returned {response.status_code}"
    assert audit(response.text) == []


def test_form_result_pages_pass_structural_audit(
    auth_client: TestClient, populated: dict[str, str]
) -> None:
    token = auth_client.csrf
    message_url = populated["message detail"]
    pages = {
        "search results": auth_client.post("/search", data={"q": "school", "csrf_token": token}),
        "original": auth_client.post(
            f"{message_url}/original", data={"csrf_token": token, "confirm": "yes"}
        ),
        "reply error": auth_client.post(
            f"{message_url}/reply", data={"instruction": "", "csrf_token": token}
        ),
        "draft error": auth_client.post(
            populated["draft"], data={"action": "review", "body": "", "csrf_token": token}
        ),
        "export error": auth_client.post(
            "/export", data={"start_date": "x", "end_date": "y", "csrf_token": token}
        ),
        "account error": auth_client.post(
            "/account/password",
            data={
                "current_password": "wrong",
                "new_password": "x",
                "confirm_password": "x",
                "csrf_token": token,
            },
        ),
        "not found": auth_client.get("/messages/00000000-0000-0000-0000-000000000000"),
        "csrf error": auth_client.post("/search", data={"q": "x"}),
    }
    for name, response in pages.items():
        assert audit(response.text) == [], name


def test_signed_out_pages_pass_structural_audit(client: TestClient, owner: User) -> None:
    login = client.get("/login")
    assert audit(login.text) == []
    bad = client.post("/login", data={"username": "a", "password": "b", "csrf_token": "x"})
    assert audit(bad.text) == []
    sign_in(client, complete_mfa=False)
    assert audit(client.get("/login/verify").text) == []


def test_mfa_setup_does_not_depend_on_a_qr_code(
    client: TestClient, db: Session, services: Services
) -> None:
    from project_buffer.services import auth

    auth.create_owner(db, "fresh", "a long enough password")
    db.commit()
    page = client.get("/login")
    token = BeautifulSoup(page.text, "html.parser").find("input", {"name": "csrf_token"})["value"]
    client.post(
        "/login",
        data={"username": "fresh", "password": "a long enough password", "csrf_token": token},
    )
    setup = client.get("/account/mfa/setup")
    assert audit(setup.text) == []
    soup = BeautifulSoup(setup.text, "html.parser")
    assert soup.find("img") is None and soup.find("canvas") is None
    assert soup.find("a", href=re.compile(r"^otpauth://totp/"))
    assert soup.find("code", class_="secret").get_text(strip=True)


def test_inbox_card_content_and_action_names(
    auth_client: TestClient, populated: dict[str, str]
) -> None:
    soup = BeautifulSoup(auth_client.get("/inbox").text, "html.parser")
    cards = soup.find_all("article")
    assert len(cards) >= 3

    # Urgency is conveyed in text, first, not by colour.
    assert _name(cards[0].find("h3")) == "Emergency: Sam injured at school"

    card = next(c for c in cards if "School transportation" in _name(c.find("h3")))
    text = _name(card)
    for expected in (
        "Jordan,",
        "Transportation to and from school on Wednesday.",
        "Her father is unavailable on Wednesday.",
        "Response needed: Yes.",
        "Personal commentary: omitted.",
    ):
        assert expected in text
    # Each card is a labelled region named by its heading.
    assert card["aria-labelledby"] == card.find("h3")["id"]
    names = [_name(element) for element in card.select(".actions a, .actions button")]
    assert any(
        name.startswith("Reply to message about School transportation from Jordan")
        for name in names
    )
    assert any(
        name.startswith("Mark handled: message about School transportation") for name in names
    )
    assert any(
        name.startswith("View details of message about School transportation") for name in names
    )
    assert any(
        name.startswith("View original of message about School transportation") for name in names
    )
    assert "Open" not in names and "View" not in names


def test_current_page_and_filter_are_marked(
    auth_client: TestClient, populated: dict[str, str]
) -> None:
    soup = BeautifulSoup(auth_client.get("/inbox?filter=urgent").text, "html.parser")
    main_nav = soup.find("nav", {"aria-label": "Main"})
    assert _name(main_nav.find("a", {"aria-current": "page"})).startswith("Inbox")
    filters = soup.find("nav", {"aria-label": "Inbox filters"})
    assert _name(filters.find("a", {"aria-current": "true"})).startswith("Urgent")


def test_notice_and_errors_are_announced(
    auth_client: TestClient, populated: dict[str, str]
) -> None:
    notice_page = auth_client.get("/inbox?notice=handled")
    soup = BeautifulSoup(notice_page.text, "html.parser")
    notice = soup.find(id="notice")
    assert notice["role"] == "status" and notice.has_attr("data-focus-on-load")
    assert soup.title.get_text().startswith("Marked as handled.")
    # Arbitrary text in the URL can never become a notice.
    injected = auth_client.get("/inbox?notice=<b>hello</b>")
    assert "hello" not in injected.text

    error_page = auth_client.post(
        populated["draft"], data={"action": "review", "body": "", "csrf_token": auth_client.csrf}
    )
    error = BeautifulSoup(error_page.text, "html.parser").find(id="error")
    assert error["role"] == "alert" and error.has_attr("data-focus-on-load")
    field = BeautifulSoup(error_page.text, "html.parser").find(id="body")
    assert field["aria-invalid"] == "true"


def test_live_region_exists_for_asynchronous_updates(
    auth_client: TestClient, populated: dict[str, str]
) -> None:
    soup = BeautifulSoup(auth_client.get("/inbox").text, "html.parser")
    live = soup.find(id="live-region")
    assert live["aria-live"] == "polite"
    banner = soup.find(id="new-messages")
    assert banner["role"] == "status" and banner.has_attr("hidden")
    assert banner["data-unread"].isdigit()


def test_send_button_is_described_by_the_exact_text(
    auth_client: TestClient, populated: dict[str, str]
) -> None:
    soup = BeautifulSoup(auth_client.get(populated["draft review"]).text, "html.parser")
    button = soup.find("button", string=re.compile("Approve and send"))
    assert button["aria-describedby"] == "exact-text"
    assert soup.find(id="exact-text").get_text() == "We can't do Wednesday."
