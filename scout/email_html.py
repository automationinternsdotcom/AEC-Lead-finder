"""HTML fragments for the daily AEC lead email; layout mirrors GPS."""
from __future__ import annotations

import hashlib
import html
import os
import re

GREEN, DARK, MUTE, BLUE = "#15704e", "#173c30", "#4f675d", "#2467a2"
TH = 'style="padding:11px 12px;font-size:10px;letter-spacing:.7px;text-transform:uppercase;border-bottom:1px solid #dfe7e3"'


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def feedback_key(name, unique=""):
    digest = hashlib.sha1(str(unique or name).encode()).hexdigest()[:8]
    base = slug(name)
    return f"{base}-{digest}" if base else digest


def buttons(key):
    feedback_base = os.environ.get("AETHER_FEEDBACK_BASE_URL", "").strip().rstrip("/")
    if not feedback_base:
        return ""
    base = f"{feedback_base}/{key}"
    style = "display:inline-block;border-radius:7px;color:#ffffff;font-size:11px;line-height:14px;font-weight:800;text-decoration:none"
    return (
        f'<div style="margin-top:10px"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tbody>'
        f'<tr><td><a href="{base}/good" style="padding:8px 12px;background:{GREEN};{style}" target="_blank">Good opportunity</a></td></tr>'
        f'<tr><td style="padding-top:10px"><a href="{base}/not-fit" style="padding:7px 11px;border:1px solid #b42318;background:#b42318;{style}" target="_blank">Not a fit</a></td></tr>'
        f"</tbody></table></div>"
    )


def contact_cell(people):
    if not any(p.get("email") or p.get("phone") or p.get("linkedin") or p.get("person") for p in people):
        return '<span style="color:#728078">No contact identified</span>'
    out = []
    for p in people:
        person = p.get("person", "")
        if not person:
            continue
        email, phone, linkedin = p.get("email", ""), p.get("phone", ""), p.get("linkedin", "")
        links = " &#183; ".join(
            l for l in (
                f'<a href="mailto:{html.escape(email, quote=True)}" style="color:{BLUE};text-decoration:none">{html.escape(email)}</a>' if email else "",
                f'<a href="tel:{re.sub(r"[^0-9]", "", phone)}" style="color:{BLUE};text-decoration:none">{html.escape(phone)}</a>' if phone else "",
                f'<a href="{html.escape(linkedin, quote=True)}" style="color:{BLUE};text-decoration:none" target="_blank">LinkedIn</a>' if linkedin else "",
            ) if l
        )
        out.append(
            f'<div style="margin:0 0 10px"><strong style="color:{DARK}">{html.escape(person)}</strong>'
            f'<span style="color:#50665d"> &#8212; {html.escape(p.get("title", ""))}</span>'
            + (f'<br><span style="font-size:12px;line-height:1.55">{links}</span>' if links else "")
            + "</div>"
        )
    return "".join(out)


def lead_row(row, people, first, shade):
    name = row.get("business_name", "")
    top = "" if first else "border-top:1px solid #e5ece8;"
    score, event, link = row.get("score", ""), row.get("event", ""), row.get("link", "")
    return (
        f'<tr style="background:{shade};vertical-align:top">'
        f'<td style="padding:14px 10px;font-size:15px;font-weight:800;color:{GREEN};{top}">{score}</td>'
        f'<td style="padding:14px 12px;{top}"><strong style="font-size:14px;color:{DARK}">{html.escape(name)}</strong><br>'
        f'<span style="font-size:12px;line-height:1.48;color:{MUTE}"><strong>{html.escape(row.get("priority", "").title())}</strong> '
        f'{html.escape(event[:100])} &#8212; {html.escape(row.get("location", ""))}.</span><br>'
        f'<a href="{html.escape(link, quote=True)}" style="display:inline-block;margin-top:5px;color:{BLUE};text-decoration:none;font-size:12px" target="_blank">{html.escape(row.get("summary", "")[:110])}</a>'
        f'<div style="margin-top:6px;font-size:11px;line-height:1.4;color:{MUTE}"><strong>Aether angle:</strong> {html.escape(row.get("service_angle", ""))}</div>'
        f'{buttons(feedback_key(name, row.get("lead_event_id") or row.get("link")))}</td>'
        f'<td style="padding:14px 12px;{top}">{contact_cell(people)}</td></tr>'
    )


def table(rows, contacts):
    body = "".join(
        lead_row(
            r,
            contacts.get(r.get("lead_event_id") or r["business_name"], []),
            i == 0,
            "#ffffff" if i % 2 == 0 else "#fbfdfc",
        )
        for i, r in enumerate(rows)
    )
    return (
        f'<table role="presentation" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:separate;border-spacing:0;border:1px solid #dfe7e3;border-radius:12px;overflow:hidden;font-family:Arial,sans-serif;font-size:13px">'
        f'<thead><tr style="background:#eff5f2;color:#456258;text-align:left"><th width="10%" {TH}>Score</th>'
        f'<th {TH}>Business &amp; property signal</th><th width="35%" {TH}>Contacts</th>'
        f"</tr></thead><tbody>{body}</tbody></table>"
    )


def section(kicker, title, blurb, rows, contacts, styles=""):
    return (
        f'<tr><td style="padding:30px 44px 34px;{styles}">'
        f'<div style="font-size:11px;line-height:1.2;letter-spacing:1.1px;text-transform:uppercase;color:#2d755b;font-weight:bold">{kicker}</div>'
        f'<h2 style="margin:7px 0 5px;color:{DARK};font-size:25px;line-height:1.25;font-weight:700">{title}</h2>'
        f'<p style="margin:0 0 18px;color:#61736c;font-size:13px;line-height:1.5">{blurb}</p>{table(rows, contacts)}</td></tr>'
    )


def status_bar(priority_count=0, nurture_count=0, contact_count=0):
    items = (
        ("Priority", priority_count),
        ("Review", nurture_count),
        ("Contacts", contact_count),
    )
    cells = "".join(
        f'<td style="padding:0 14px 0 0;white-space:nowrap"><span style="display:block;color:#a8e0c5;font-size:10px;letter-spacing:.8px;text-transform:uppercase;font-weight:bold">{label}</span><strong style="color:#ffffff;font-size:18px;line-height:1.2">{value}</strong></td>'
        for label, value in items
    )
    return (
        f'<table role="presentation" cellspacing="0" cellpadding="0" style="margin-top:16px;border-collapse:collapse"><tbody><tr>{cells}</tr></tbody></table>'
    )


def page(sections, priority_count=0, nurture_count=0, contact_count=0, sales_handoff=True):
    feedback_copy = (
        f'<div style="margin-top:10px;color:#e8f4ef;font-size:13px;line-height:1.55">After reviewing an article, click <strong style="color:#a8e0c5">Good opportunity</strong> or <strong style="color:#ffb7aa">Not a fit</strong>; each choice helps improve future lead scoring.</div>'
        if os.environ.get("AETHER_FEEDBACK_BASE_URL", "").strip()
        else '<div style="margin-top:10px;color:#e8f4ef;font-size:13px;line-height:1.55">Daily reviewed AEC property signals, with outreach readiness shown by section.</div>'
    )
    handoff_copy = (
        '<div style="margin-top:12px;color:#d9eee7;font-size:12px;line-height:1.5">Sales handoff: qualified lead records and selected contacts were prepared for ingestion into Pipedrive and WarmySender.</div>'
        if sales_handoff
        else ""
    )
    return (
        f'<div style="margin:0;padding:0;background:#eef3f1;color:#233a32">'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="width:100%;background:#eef3f1"><tbody><tr><td align="center" style="padding:28px 12px">'
        f'<table role="presentation" width="760" cellspacing="0" cellpadding="0" style="width:760px;max-width:100%;background:#ffffff;border-collapse:separate;border-spacing:0;border-radius:18px;overflow:hidden;font-family:Arial,sans-serif"><tbody>'
        f'<tr><td style="background:#123a2d;padding:22px 44px">'
        f'<div style="font-size:12px;line-height:1.2;letter-spacing:1.4px;text-transform:uppercase;color:#a8e0c5;font-weight:bold">Aether AEC Lead Intelligence</div>'
        f'{feedback_copy}'
        f'{status_bar(priority_count, nurture_count, contact_count)}'
        f'{handoff_copy}'
        f"</td></tr>{sections}"
        f'<tr><td style="padding:23px 44px 27px;background:#ffffff;border-top:1px solid #e0ebe6;color:{MUTE};font-size:13px;line-height:1.6">'
        f"<div>Generated from curated AEC news websites, Grok research artifacts, and the V2 sales handoff export.</div>"
        f"</td></tr></tbody></table></td></tr></tbody></table></div>"
    )
