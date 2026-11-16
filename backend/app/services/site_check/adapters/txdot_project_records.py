"""Bounded official-record enrichment for TxDOT Project Tracker rows."""

from __future__ import annotations

import re
import warnings
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin

import requests
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from urllib3.exceptions import InsecureRequestWarning

PROJECT_TRACKER_URL = "https://apps3.txdot.gov/apps-cq/project_tracker/"
PLANS_ONLINE_URL = "https://www.txdot.gov/business/plans-online-bid-lettings.html"
_CONTRACT_REPORT_URL = (
    "https://ftp.txdot.gov/pub/txdot-info/cmd/cserve/distinfo/cisrpts/{csj}.pdf"
)
_PROPOSAL_URL = (
    "https://ftp.txdot.gov/pub/txdot-info/cmd/txctesttxdotgov/proposal/"
    "preprod/{district}/{project_id}/PR_{project_id}.pdf"
)
_ADDENDA_URL = (
    "https://ftp.txdot.gov/pub/txdot-info/cmd/txctesttxdotgov/addendum/"
    "preprod/{district}/{project_id}/"
)
_TIMEOUT_S = 12
_MAX_PDF_BYTES = 8_000_000
_MAX_ADDENDA = 4


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def enrich_project_official_records(
    project: dict,
    session: requests.Session,
) -> dict:
    """Attach facts only when a corresponding official TxDOT record resolves."""
    enriched = dict(project)
    documents: list[dict[str, str]] = []
    csj = _digits(project.get("csj"))
    district = _district(project.get("district_number"))
    project_id = str(project.get("project_id") or "").strip()

    award = _contract_report(session, csj) if len(csj) == 9 else None
    if award:
        estimate = project.get("estimated_cost_usd")
        amount = award.get("award_amount_usd")
        if isinstance(estimate, (int, float)) and estimate and isinstance(amount, float):
            award["award_variance_pct"] = round((amount - estimate) / estimate * 100, 2)
        enriched["award"] = award
        documents.append({
            "kind": "contract_estimate",
            "label": "TxDOT contractor estimate package",
            "url": _CONTRACT_REPORT_URL.format(csj=csj),
            "status": "verified_direct_pdf",
        })

    if district and project_id:
        proposal_url = _PROPOSAL_URL.format(
            district=district,
            project_id=project_id,
        )
        if _verified_pdf(session, proposal_url):
            documents.append({
                "kind": "proposal",
                "label": "TxDOT project proposal",
                "url": proposal_url,
                "status": "verified_direct_pdf",
            })

        addenda_url = _ADDENDA_URL.format(
            district=district,
            project_id=project_id,
        )
        addenda = _addenda(session, addenda_url)
        if addenda:
            enriched["addenda"] = addenda
            documents.extend({
                "kind": "addendum",
                "label": f"TxDOT addendum {row['number']}",
                "url": row["url"],
                "status": "verified_direct_pdf",
            } for row in addenda)

    enriched["documents"] = documents
    enriched["official_lookup_links"] = [
        {
            "kind": "project_tracker",
            "label": "TxDOT Project Tracker",
            "url": PROJECT_TRACKER_URL,
        },
        {
            "kind": "plans_online",
            "label": "TxDOT Plans Online",
            "url": PLANS_ONLINE_URL,
        },
    ]
    enriched["document_lookup"] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "direct_document_count": len(documents),
        "direct_plan_status": "not_found_in_automatic_public_lookup",
        "direct_plan_advisory": (
            "No direct plan-sheet or schematic URL was found by the automatic public lookup. "
            "TxDOT Plans Online may still hold a plan set; search by CSJ."
        ),
    }
    return enriched


def _contract_report(session: requests.Session, csj: str) -> dict | None:
    url = _CONTRACT_REPORT_URL.format(csj=csj)
    text = _download_pdf_text(session, url, page_limit=1)
    if not text or csj not in _digits(text):
        return None
    award_amount = _money(text, "AWARD AMOUNT")
    if award_amount is None:
        return None
    return {
        "contractor": _value(text, "CONTRACTOR"),
        "contract_number": _value(text, "CONTRACT", numeric=True),
        "award_amount_usd": award_amount,
        "letting_date": _date(text, "LETTING DATE"),
        "award_date": _date(text, "AWARD DATE"),
        "notice_to_proceed_date": _date(text, "NOTICE TO PROCEED DATE"),
        "work_begin_date": _date(text, "WORK BEGIN DATE"),
        "accepted_date": _date(text, "ACCEPTED DATE"),
        "percent_complete": _number(text, "% COMPLETE"),
        "paid_to_contractor_usd": _money(text, "PAID TO CONTRACTOR"),
        "source": "TxDOT contractor estimate package",
    }


def _addenda(session: requests.Session, directory_url: str) -> list[dict]:
    try:
        response = _official_request(session, "get", directory_url)
        response.raise_for_status()
    except requests.RequestException:
        return []
    parser = _LinkParser()
    parser.feed(response.text)
    urls = [
        urljoin(directory_url, href)
        for href in parser.links
        if href.lower().endswith(".pdf")
    ][:_MAX_ADDENDA]
    output = []
    for index, url in enumerate(urls, start=1):
        text = _download_pdf_text(session, url, page_limit=2)
        if not text:
            continue
        number_match = re.search(r"ADDENDUM\s+NO\.?\s*(\d+)", text, re.IGNORECASE)
        date_match = re.search(r"DATED\s+(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
        changes = _addendum_changes(text)
        output.append({
            "number": number_match.group(1) if number_match else str(index),
            "date": _iso_date(date_match.group(1)) if date_match else None,
            "changes": changes,
            "summary": "; ".join(changes) if changes else "Official addendum posted",
            "url": url,
        })
    return output


def _addendum_changes(text: str) -> list[str]:
    output: list[str] = []
    for item, action in re.findall(
        r"(BID ITEM\s+[A-Z0-9-]+|SPECIAL SPECIFICATION\s+[A-Z0-9-]+)\s+"
        r"WAS\s+(ADDED|CHANGED|DELETED)",
        text,
        re.IGNORECASE,
    ):
        label = f"{item.upper()} {action.lower()}"
        if label not in output:
            output.append(label)
    if re.search(r"\*{3,}\s*GENERAL NOTES\s*\*{3,}\s*Adding", text, re.IGNORECASE):
        output.append("General notes changed")
    return output[:12]


def _verified_pdf(session: requests.Session, url: str) -> bool:
    try:
        response = _official_request(session, "head", url, allow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        return "pdf" in content_type
    except requests.RequestException:
        return False


def _download_pdf_text(
    session: requests.Session,
    url: str,
    *,
    page_limit: int,
) -> str | None:
    try:
        response = _official_request(session, "get", url)
        response.raise_for_status()
        body = response.content
        if len(body) > _MAX_PDF_BYTES or not body.startswith(b"%PDF"):
            return None
        reader = PdfReader(BytesIO(body))
        page_text = []
        for page in reader.pages[:page_limit]:
            layout = page.extract_text(extraction_mode="layout") or ""
            page_text.append(layout if layout.strip() else (page.extract_text() or ""))
        return "\n".join(page_text)
    except (OSError, PdfReadError, ValueError, requests.RequestException):
        return None


def _official_request(
    session: requests.Session,
    method: str,
    url: str,
    **kwargs,
) -> requests.Response:
    """Use verified TLS first; TxDOT's FTP host intermittently omits its chain."""
    kwargs.setdefault("timeout", _TIMEOUT_S)
    request = getattr(session, method)
    try:
        return request(url, **kwargs)
    except requests.exceptions.SSLError:
        if not url.startswith("https://ftp.txdot.gov/"):
            raise
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            return request(url, verify=False, **kwargs)


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _district(value: object) -> str | None:
    try:
        return f"{int(float(str(value))):02d}"
    except (TypeError, ValueError):
        return None


def _value(text: str, label: str, *, numeric: bool = False) -> str | None:
    if numeric:
        pattern = rf"(?m)^\s*{re.escape(label)}:\s+(\d+)\s+"
    else:
        pattern = rf"(?m)(?:^|:)\s*{re.escape(label)}:\s+([^\n]+?)\s*$"
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _date(text: str, label: str) -> str | None:
    match = re.search(
        rf"{re.escape(label)}:\s+(\d{{2}}/\d{{2}}/\d{{4}})",
        text,
        re.IGNORECASE,
    )
    return _iso_date(match.group(1)) if match else None


def _iso_date(value: str) -> str | None:
    try:
        return datetime.strptime(value, "%m/%d/%Y").replace(tzinfo=UTC).date().isoformat()
    except ValueError:
        return None


def _money(text: str, label: str) -> float | None:
    match = re.search(
        rf"{re.escape(label)}:?\s+\$([\d,]+(?:\.\d{{2}})?)",
        text,
        re.IGNORECASE,
    )
    return float(match.group(1).replace(",", "")) if match else None


def _number(text: str, label: str) -> float | None:
    match = re.search(
        rf"{re.escape(label)}:\s+([\d.]+)",
        text,
        re.IGNORECASE,
    )
    return float(match.group(1)) if match else None


__all__ = [
    "PLANS_ONLINE_URL",
    "PROJECT_TRACKER_URL",
    "enrich_project_official_records",
]
