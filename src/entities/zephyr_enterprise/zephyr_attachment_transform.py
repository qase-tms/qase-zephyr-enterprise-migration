"""
Zephyr Enterprise → Qase attachment text transforms.

The flow: upload the file, fill an id→{hash, url} map, rewrite the in-text
links to point at Qase, then merge the hashes onto the case payload.
"""

from __future__ import annotations

import html
import os
import re
from typing import Dict, List, Optional, Tuple

# Relative /flex/download and absolute URLs up to query string (Zephyr UI / REST).
_FLEX_DOWNLOAD_WITH_FILEID = re.compile(
    r"(?i)(?:https?://[^\s\"'<>]+)?/flex/download\?"
    r"[^\s\"'<>]*(?:fileId|fileid)=([A-Za-z0-9_.-]+)[^\s\"'<>]*"
)

# <img ... src="..."> — tolerant of attribute order and optional self-close.
_IMG_TAG_WITH_SRC = re.compile(
    r"(?is)<img\b[^>]*?\bsrc\s*=\s*(?P<q>[\"'])(?P<src>.*?)(?P=q)[^>]*?/?>"
)

_IMG_SRC_QASE_ATTACHMENT = re.compile(
    r"(?is)<img\b[^>]*?\bsrc\s*=\s*(?P<q>[\"'])(?P<src>https?://[^\"']+/attachment/[^\"']+)(?P=q)[^>]*?/?>"
)


def _lookup_fid_maps(
    fid: str, file_id_to_url: Dict[str, str]
) -> Tuple[Optional[str], str]:
    """Return (canonical_key_or_none, url) for flex file id (case-insensitive)."""
    url = file_id_to_url.get(fid)
    if url:
        return fid, url
    for k, v in file_id_to_url.items():
        if k.lower() == fid.lower():
            return k, v
    return None, ""


def _label_for_fid(
    fid: str,
    file_id_to_label: Dict[str, str],
    url: str,
    alt_from_tag: str,
) -> str:
    if alt_from_tag and alt_from_tag.strip():
        return alt_from_tag.strip()
    lb = file_id_to_label.get(fid)
    if not lb and fid:
        for k, v in file_id_to_label.items():
            if k.lower() == fid.lower():
                lb = v
                break
    if lb and lb.strip():
        return lb.strip()
    return _basename_for_markdown(url)


def _basename_for_markdown(url_or_name: str) -> str:
    s = (url_or_name or "").replace("\\", "/").rstrip("/").split("/")[-1]
    s = s.split("?")[0].strip()
    return s or "attachment"


def _markdown_attachment_inline(label: str, url: str) -> str:
    """Qase-rich-text friendly: images as ![alt](url), other files as [alt](url)."""
    safe = (label or "attachment").replace("\n", " ").strip() or "attachment"
    ext = os.path.splitext(safe)[1].lower()
    if ext in (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".bmp",
        ".ico",
    ):
        return f"![{safe}]({url})"
    return f"[{safe}]({url})"


def _collect_hash_for_fid(
    fid: str, hm: Dict[str, str], collected: List[str]
) -> None:
    h = hm.get(fid)
    if not h and fid:
        for k, v in hm.items():
            if k.lower() == fid.lower():
                h = v
                break
    if h and h not in collected:
        collected.append(h)


def replace_zephyr_flex_urls_in_text(
    text: str,
    file_id_to_url: Dict[str, str],
    file_id_to_hash: Optional[Dict[str, str]] = None,
    file_id_to_label: Optional[Dict[str, str]] = None,
) -> Tuple[str, List[str]]:
    """
    Rewrite Zephyr flex attachment references to Qase-friendly **markdown** (not raw ``<img src>``).

    1. ``<img src=".../flex/download?...fileId=…">`` → ``![filename](qase_url)``
    2. Bare flex URLs → same markdown form
    3. Leftover ``<img src="https://…/attachment/…">`` → markdown (fixes broken HTML img tags in Qase)
    """
    if not text:
        return text or "", []

    # Zephyr often stores rich text as HTML-escaped literals (&lt;p&gt;, &#34; for quotes).
    out = html.unescape(text)
    hm = file_id_to_hash or {}
    lm = file_id_to_label or {}
    collected: List[str] = []

    if not file_id_to_url:
        # No flex map (e.g. skipped upload) — still normalize Qase CDN <img> tags if present.
        if "<img" not in out.lower() or "/attachment/" not in out.lower():
            return out, []

        def qase_only_replacer(m: re.Match) -> str:
            tag = m.group(0)
            src = m.group("src").replace("&amp;", "&")
            if "/attachment/" not in src.lower():
                return tag
            alt_m = re.search(r"\salt\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
            alt_from_tag = alt_m.group(1) if alt_m else ""
            label = (
                alt_from_tag.strip()
                if alt_from_tag and alt_from_tag.strip()
                else _basename_for_markdown(src)
            )
            return _markdown_attachment_inline(label, src)

        out = _IMG_SRC_QASE_ATTACHMENT.sub(qase_only_replacer, out)
        return out, collected


    def img_flex_replacer(m: re.Match) -> str:
        tag = m.group(0)
        src = m.group("src").replace("&amp;", "&")
        flex_m = _FLEX_DOWNLOAD_WITH_FILEID.search(src)
        if not flex_m:
            return tag
        raw_fid = flex_m.group(1)
        key, url = _lookup_fid_maps(raw_fid, file_id_to_url)
        if not url or not key:
            return tag
        alt_m = re.search(r"\salt\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        alt_from_tag = alt_m.group(1) if alt_m else ""
        label = _label_for_fid(key, lm, url, alt_from_tag)
        _collect_hash_for_fid(key, hm, collected)
        return _markdown_attachment_inline(label, url)

    out = _IMG_TAG_WITH_SRC.sub(img_flex_replacer, out)

    def bare_flex_replacer(match: re.Match) -> str:
        raw_fid = match.group(1)
        key, url = _lookup_fid_maps(raw_fid, file_id_to_url)
        if not url or not key:
            return match.group(0)
        _collect_hash_for_fid(key, hm, collected)
        label = _label_for_fid(key, lm, url, "")
        return _markdown_attachment_inline(label, url)

    out = _FLEX_DOWNLOAD_WITH_FILEID.sub(bare_flex_replacer, out)

    def qase_img_replacer(m: re.Match) -> str:
        tag = m.group(0)
        src = m.group("src").replace("&amp;", "&")
        if "/attachment/" not in src.lower():
            return tag
        alt_m = re.search(r"\salt\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        alt_from_tag = alt_m.group(1) if alt_m else ""
        label = (
            alt_from_tag.strip()
            if alt_from_tag and alt_from_tag.strip()
            else _basename_for_markdown(src)
        )
        return _markdown_attachment_inline(label, src)

    out = _IMG_SRC_QASE_ATTACHMENT.sub(qase_img_replacer, out)

    return out, collected


def merge_zephyr_case_attachment_hashes(
    case_level_hashes: List[str],
    inline_hashes: List[str],
) -> List[str]:
    """Dedupe while preserving order (case-level first, then inline-derived)."""
    seen = set()
    merged: List[str] = []
    for h in (case_level_hashes or []) + (inline_hashes or []):
        if h and h not in seen:
            seen.add(h)
            merged.append(h)
    return merged
