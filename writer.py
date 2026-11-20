"""Stage 3: multi-pass content generation (draft -> critique -> revision).

The critique is fully deterministic and always runs as the final gate. Drafting backends:
  - "openrouter": a writer model and an editor model alternate (needs OPENROUTER_API_KEY).
                  writer drafts -> editor returns a fix list -> writer revises -> ... -> final gate.
  - "anthropic":  Claude writes and revises (needs ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN).
  - "template":   deterministic composer built only from the brief (no extra key needed).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date

from dotenv import load_dotenv

import lang
from apiclient import ApiError, TruncatedError, openrouter_chat
from config import ROOT, get_runtime, optional_secret
from textutil import (BANNED_PHRASES, PLACEHOLDER_PATTERNS, banned_phrases, build_frontmatter, content_year,
                      contains_keyword, disclosure_patterns, find_banned, is_faq_heading, jaccard, normalize_quotes,
                      drop_past_years, past_years, plain_text, scrub_banned, smart_cap, split_sections, tokens, word_count)

CORPORATE = ["utilize", "synergy", "synergies", "stakeholder", "best-in-class", "paradigm", "holistic",
             "empower", "revolutionize", "game-changer", "game changer", "world-class", "value-added",
             "solutions provider", "mission-critical", "next-generation", "state-of-the-art"]
ANALOGY = re.compile(
    r"\b(think of|imagine|picture (this|a|an|the)|it'?s like|is like|are like|works like|work like|acts like|"
    r"like a |like an |the same way|analogy|mental model|rule of thumb|"
    r"(principle|effect|rule|law|razor|model|framework|paradox|test|method|curve|loop)\b)", re.I)
NON_MAJOR = re.compile(r"^(frequently asked|faq|where to go|next steps?|what to do next|sources|further reading|"
                       r"key takeaways|takeaways|tl;?dr)", re.I)


class WriterError(Exception):
    def __init__(self, message, fixes=None):
        super().__init__(message)
        self.fixes = fixes or []


# =====================================================================
# Critique (deterministic) -> fix list
# =====================================================================

def _match_section(sections, heading):
    want = set(tokens(heading))
    best, score = None, 0.0
    for s in sections:
        j = jaccard(want, set(tokens(s["h2"])))
        if j > score:
            best, score = s, j
    return best if score >= 0.6 else None


def _sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if word_count(s) > 0]


def year_rule(config: dict) -> str:
    y = content_year(config)
    if lang.is_es():
        return (f"- Escribe para {y}. Nunca menciones un año anterior a {y} (ni en el título, los encabezados, las "
                f"preguntas frecuentes ni el texto), aunque aparezca en la investigación: di \"hoy\" o \"en {y}\".")
    return (f"- Write for {y}. Never name a year before {y} (in the title, headings, FAQ or body), even when the research "
            f"or a ranking page does: say \"today\", \"currently\" or \"in {y}\" instead. Link URLs may contain dates.")


def critique(article: dict, brief: dict, config: dict) -> list[dict]:
    """Return the list of required fixes ([] means the piece passes)."""
    fixes = []

    def fix(code, message, **kw):
        fixes.append({"code": code, "message": message, **kw})

    import evidence as _ev
    import onpage as _op
    _ev.attach(brief, config)   # idempotent: generate_article attaches first (with the refresh flag)
    _op.attach(brief, config)
    kw = brief["primary_keyword"]
    title, desc, body = article["title"], article["description"], article["body"]
    h1, intro, sections = split_sections(body)
    text = plain_text(body)
    wc = word_count(text)
    target = brief["target_word_count"]

    if wc < target:
        fix("word_count", f"Body is {wc} words; needs at least {target}. Add {target - wc}+ words of substance "
                          f"to the thinnest sections.", have=wc, need=target)

    max_f = config.get("word_count_max_factor")
    if max_f and wc > target * max_f:
        fix("word_count_max", f"Body is {wc} words; keep it under {int(target * max_f)}. Cut {wc - int(target * max_f)}+ words "
                              f"by trimming repetition and the least useful section detail.", have=wc, limit=int(target * max_f))

    es = lang.is_es()
    non_major, analogy = (lang.ES_NON_MAJOR, lang.ES_ANALOGY) if es else (NON_MAJOR, ANALOGY)
    import evidence
    major = [s for s in sections if not non_major.search(s["h2"]) and not evidence.is_evidence_heading(s["h2"])]
    for s in major:
        if not analogy.search(normalize_quotes(s["text"])):
            fix("analogy_missing", f'Section "{s["h2"]}" has no analogy or named mental model. Add one strong one.',
                section=s["h2"])

    full = "\n".join([title, desc, body])
    for phrase in find_banned(full):
        fix("banned_phrase", f'Remove the banned phrase "{phrase}" everywhere.', phrase=phrase)
    for pat in disclosure_patterns():
        if re.search(pat, full, flags=re.I):
            fix("disclosure", "Remove any statement about how the text was produced.", pattern=pat)
    for pat in PLACEHOLDER_PATTERNS:
        m = re.search(pat, re.sub(r"```.*?```", "", full, flags=re.S))
        if m:
            fix("placeholder", f'Replace placeholder text "{m.group(0)}" with real content.', match=m.group(0))

    year = content_year(config)
    if old := past_years(full, year):
        fix("past_year", f"Remove every mention of {', '.join(old)}: the page is written for {year} and names no "
                         f"earlier year. Say \"today\" or \"in {year}\", or drop the date.", years=old, content_year=year)

    if not contains_keyword(title, kw):
        fix("keyword_title", f'Title must contain the primary keyword "{kw}".')
    if config.get("include_h1_in_body", True):
        if not h1:
            fix("h1_missing", "Body must start with a single H1.")
        elif not contains_keyword(h1, kw):
            fix("keyword_h1", f'H1 must contain the primary keyword "{kw}".')
    first100 = " ".join(plain_text(re.sub(r"^#\s.*$", "", body, count=1, flags=re.M)).split()[:100])
    if not contains_keyword(first100, kw):
        fix("keyword_first_100", f'Use the primary keyword "{kw}" within the first 100 words.')
    if not contains_keyword(desc, kw):
        fix("keyword_description", f'Meta description must contain the primary keyword "{kw}".')
    if not 50 <= len(desc) <= 160:
        fix("description_length", f"Meta description is {len(desc)} characters; keep it between 50 and 160.")
    if word_count(plain_text(intro)) > 150:
        fix("intro_too_long", f"Introduction is {word_count(plain_text(intro))} words; cut it to 150 or fewer.")

    faq = next((s for s in sections if is_faq_heading(s["h2"])), None)
    faq_qs = re.findall(r"^###\s+(.+)$", faq["text"], flags=re.M) if faq else []
    for gap in brief["gaps_to_address"]:
        is_faq = is_faq_heading(gap["section"])
        if is_faq:
            qt = set(tokens(gap["topic"]))
            if not any(jaccard(qt, set(tokens(q))) >= 0.6 for q in faq_qs):
                fix("gap_not_covered", f'Answer "{gap["topic"]}" as its own H3 in the FAQ. Why: {gap["reason"]}',
                    gap=gap["topic"], section=gap["section"])
            continue
        sec = _match_section(sections, gap["section"])
        need = 250 if gap["type"] == "thin_coverage" else 80
        if sec is None:
            fix("gap_not_covered", f'Add an H2 section "{gap["section"]}" ({need}+ words). Why: {gap["reason"]}',
                gap=gap["topic"], section=gap["section"])
        elif word_count(plain_text(sec["text"])) < need:
            fix("gap_too_thin", f'Section "{sec["h2"]}" must be at least {need} words. Why: {gap["reason"]}',
                gap=gap["topic"], section=sec["h2"], need=need)

    need_q = min(3, len(next((o["h3"] for o in brief["outline"] if o.get("faq")), [])))
    if not faq:
        fix("faq_missing", "Add a final FAQ H2 with one H3 per People Also Ask question.")
    elif len(faq_qs) < need_q:
        fix("faq_too_short", f"FAQ needs at least {need_q} questions as H3s; it has {len(faq_qs)}.")

    has_list = re.search(r"^\s*([-*]|\d+\.)\s+\S", body, flags=re.M)
    has_table = re.search(r"^\|.+\|\s*\n\|[\s:|-]+\|", body, flags=re.M)
    if not (has_list or has_table):
        fix("no_list_or_table", "Include at least one list or table.")

    sents = _sentences(re.sub(r"^#+ .*$", "", text, flags=re.M))
    avg = sum(word_count(s) for s in sents) / max(1, len(sents))
    if avg > (28 if es else 24):   # Spanish sentences run a little longer for the same content
        fix("tone_sentences_long", f"Average sentence is {avg:.0f} words. Shorten sentences; aim for under 20.")
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip() and not p.lstrip().startswith(("#", "|", "-", "*", ">"))
             and not re.match(r"^\d+\.", p.strip())]
    singles = [p for p in paras if len(_sentences(p)) == 1 and word_count(p) <= 18]
    if len(singles) < 3:
        fix("tone_no_emphasis", f"Only {len(singles)} single-sentence paragraphs. Use at least 3 for emphasis.")
    if es:
        if len(lang.ES_DIRECT_ADDRESS.findall(text)) < 8:
            fix("tone_not_conversational", 'Speak directly to the reader in the "tú" form throughout.')
    elif len(re.findall(r"\byou(r|'re|'ll|'ve|'d)?\b", text, flags=re.I)) < 8:
        fix("tone_not_conversational", "Speak directly to the reader (you/your) throughout.")
    low = text.lower()
    for w in (lang.ES_CORPORATE if es else CORPORATE):
        if re.search(r"\b" + re.escape(w), low):
            fix("tone_corporate", f'Replace the corporate word "{w}" with plain language.', word=w)

    rules = brief.get("placement_rules", {})
    links = re.findall(r"\]\(([^)\s]+)\)", body)
    dir_prefix = config["directory_url_prefix"].rstrip("/") + "/"
    n_dir = len({l for l in links if l.startswith(dir_prefix)})
    if n_dir < rules.get("directory_links_min", 0):
        fix("directory_links", f"Add contextual links to at least {rules['directory_links_min']} directory pages "
                               f"(found {n_dir}).", have=n_dir)
    for t in brief.get("internal_link_targets", []):
        if t["type"] == "article" and t["url"] not in links:
            fix("internal_link_missing", f'Link "{t["anchor"]}" to {t["url"]} once in a body paragraph.', url=t["url"])
    ext = [s["url"] for s in brief.get("external_sources", [])]
    if sum(1 for u in ext if u in links) < min(2, len(ext)):
        fix("external_sources", "Cite at least 2 of the external sources with a link.")

    kw_h2_max = config.get("keyword_h2_max")
    if kw_h2_max is not None:
        hits = [s["h2"] for s in sections if not is_faq_heading(s["h2"]) and contains_keyword(s["h2"], kw)]
        if len(hits) > kw_h2_max:
            fix("keyword_h2_stuffing", f'The primary keyword "{kw}" appears in {len(hits)} H2 headings ({"; ".join(hits)}). '
                                       f"Keep it in at most {kw_h2_max}; rename the others to say what the section teaches.",
                have=len(hits))
    import comparisons
    comparisons.critique(article, brief, fix)
    evidence.critique(article, brief, fix)
    import onpage
    onpage.critique(article, brief, fix)
    import slop
    slop.critique(article, config, fix)
    if not 40 <= len(title) <= 60:
        fix("title_length", f"Title is {len(title)} characters; aim for 40-60 with the primary keyword in the first half, "
                            "so it is not cut off in results.", have=len(title), advisory=True)
    if brief.get("refresh"):
        import refresh
        refresh.critique(article, brief, fix)
    if config.get("forbid_best_lists") and not brief.get("comparison"):  # a comparison page names companies on purpose
        for h in re.findall(r"^#{2,3}\s+(.+)$", body, flags=re.M):
            if re.match(r"^(the\s+)?(best|top\s+\d+|top\s+(ten|five|three))\b", h.strip(), flags=re.I):
                fix("best_list", f'Heading "{h.strip()}" reads as a ranking. Do not rank or recommend named companies; '
                                 f"retitle it as how to evaluate options and keep the content about criteria.", heading=h.strip())
    return fixes


# =====================================================================
# Backend: Claude
# =====================================================================

SYSTEM_PROMPT = """You write long-form articles for {site} ({niche}).

Voice: conversational and curious. Explain layer by layer, the way Tim Urban or Packy McCormick would: start \
from the simple version, then peel back one layer at a time. Short sentences. Single-sentence paragraphs for \
emphasis. Concrete examples, real numbers and specific claims instead of vague language. Talk to the reader as "you".

Hard rules:
- Never use these phrases (any casing): {banned}.
- Never mention how the article was produced, and never refer to yourself, a brief, a prompt or these rules.
- Never invent statistics. Use only facts you are confident are true or that appear in the supplied research.
- No placeholders. No corporate jargon.
{slop_rules}
{year_rule}
- Output format, exactly:
TITLE: <title, 40-60 characters, primary keyword in the first half>
DESCRIPTION: <meta description, 120-155 characters, contains the primary keyword>
===
<the article in clean Markdown, starting with "# " H1 that contains the primary keyword>"""


def _brief_for_prompt(brief: dict) -> str:
    lines = [f'Primary keyword: {brief["primary_keyword"]}',
             f'Secondary keywords: {", ".join(brief["secondary_keywords"]) or "none"}',
             f'Search intent: {brief["intent"]}',
             f'Minimum length: {brief["target_word_count"]} words (aim for {int(brief["target_word_count"] * 1.1)})',
             "", "Outline (keep every H2, in this order; you may sharpen wording but keep the topic):"]
    for s in brief["outline"]:
        tag = " [GAP SECTION: competitors skip this, go deep]" if s.get("gap") else ""
        lines.append(f'## {s["h2"]}{tag}')
        lines += [f'### {h}' for h in s["h3"]]
    lines += ["", "Gaps you must explicitly cover:"]
    lines += [f'- {g["topic"]} (in section "{g["section"]}"): {g["reason"]}' for g in brief["gaps_to_address"]]
    lines += ["", "Placement rules:",
              "- Primary keyword in the title, the H1, the first 100 words and the meta description.",
              "- Introduction of 150 words or fewer, no generic scene-setting.",
              "- At least one strong analogy or named mental model in every H2 section except the FAQ.",
              "- At least one list or table.",
              "- The FAQ is the final H2, one H3 per question, each answer 40-90 words."]
    internal = brief.get("internal_link_targets") or []
    if internal:
        lines += ["", "Internal links (use each once, in a body paragraph, with natural anchor text):"]
        lines += [f'- [{t["anchor"]}]({t["url"]})' for t in internal]
    if brief.get("external_sources"):
        lines += ["", "External sources (cite at least two with a Markdown link):"]
        lines += [f'- {s["title"]}: {s["url"]}' for s in brief["external_sources"]]
    summ = brief.get("serp_summary", {})
    if summ.get("top_results"):
        lines += ["", "What currently ranks (for grounding; do not copy):"]
        lines += [f'- {r["title"]} ({r["domain"]}): {r["snippet"]}' for r in summ["top_results"][:8]]
    import comparisons
    import evidence
    import refresh
    import aivis
    import onpage
    return ("\n".join(lines) + comparisons.prompt_block(brief) + evidence.prompt_block(brief)
            + onpage.prompt_block(brief) + aivis.prompt_block(brief) + refresh.prompt_block(brief))


def _llm(config, system: str, user: str) -> str:
    import anthropic
    import budget
    rt = get_runtime(config)
    budget.check(config, "llm")
    client = anthropic.Anthropic(max_retries=2)
    try:
        with client.beta.messages.stream(
            model=config["llm_model"], max_tokens=32000, system=system,
            thinking={"type": "adaptive"}, output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.RateLimitError as e:
        raise WriterError(f"Claude API rate limited after retries: {e}")
    except anthropic.APIStatusError as e:
        raise WriterError(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        raise WriterError(f"Claude API connection error: {e}")
    rt.log("llm_call", model=msg.model, stop_reason=msg.stop_reason,
           input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens)
    budget.record(config, "llm", None, f"anthropic {msg.model}")  # charged at budget.llm_usd_fallback
    if msg.stop_reason == "refusal":
        raise WriterError("Claude declined to write this piece")
    if msg.stop_reason == "max_tokens":
        raise WriterError("Claude response was cut off at max_tokens")
    return "".join(b.text for b in msg.content if b.type == "text")


def _parse_llm(raw: str, fallback: dict | None = None) -> dict:
    t = re.search(r"^TITLE:\s*(.+)$", raw, flags=re.M)
    d = re.search(r"^DESCRIPTION:\s*(.+)$", raw, flags=re.M)
    body = raw.split("===", 1)[1] if "===" in raw else raw[raw.find("# "):] if "# " in raw else raw
    body = re.sub(r"^```(markdown)?\s*|\s*```\s*$", "", body.strip())
    art = {"title": (t.group(1).strip().strip('"') if t else (fallback or {}).get("title", "")),
           "description": (d.group(1).strip().strip('"') if d else (fallback or {}).get("description", "")),
           "body": body.strip() + "\n"}
    if not art["title"]:
        m = re.search(r"^#\s+(.+)$", art["body"], flags=re.M)
        art["title"] = m.group(1).strip() if m else ""
    return art


class AnthropicBackend:
    name = "anthropic"

    def _call(self, config, system, user) -> str:
        return _llm(config, system, user)

    def _system(self, config):
        import slop
        system = SYSTEM_PROMPT.format(site=config["site_name"], niche=config["niche_description"],
                                      banned=", ".join(f'"{p}"' for p in BANNED_PHRASES), slop_rules=slop.prompt_rules(), year_rule=year_rule(config))
        if lang.is_es():
            system += "\n" + lang.ES_WRITER_RULES.format(banned=", ".join(f'"{p}"' for p in banned_phrases()))
        if config.get("writer_notes"):
            system += "\n\nFacts and limits for this site (follow them exactly):\n" + str(config["writer_notes"]).strip()
        return system

    def draft(self, brief, config) -> dict:
        return _parse_llm(self._call(config, self._system(config),
                                     "Write the article from this brief.\n\n" + _brief_for_prompt(brief)))

    def revise(self, article, fixes, brief, config) -> dict:
        user = ("Revise the article below. Apply every fix in the list; change nothing that already works. "
                "Return the complete revised article in the same output format.\n\nFIXES:\n"
                + "\n".join(f'{i}. {f["message"]}' for i, f in enumerate(fixes, 1))
                + "\n\nBRIEF:\n" + _brief_for_prompt(brief)
                + f'\n\nCURRENT ARTICLE:\nTITLE: {article["title"]}\nDESCRIPTION: {article["description"]}\n===\n'
                + article["body"])
        return _parse_llm(self._call(config, self._system(config), user), fallback=article)


# =====================================================================
# Backend: OpenRouter (writer model + editor model, alternating)
# =====================================================================

EDITOR_PROMPT = """You are the commissioning editor for {site} ({niche}). A writer has submitted a draft \
written from the research brief below. You do not rewrite anything. You return a list of fixes for the writer.

Read the draft as the reader who searched for the primary keyword. Flag only problems worth a revision:
- A statistic, number, quote or named study that does not come from the brief's sources or is not common \
knowledge in the field. Tell the writer to cut it or replace it with a claim the brief supports.
- A gap section that restates what competitors already say instead of going deeper than they do.
- A section that is generic: it could sit unchanged in an article about a different topic.
- Advice with no concrete example, step, number or decision rule behind it.
- An analogy or mental model that is forced, repeated or does not clarify the point.
- Padding, repetition between sections, an introduction that delays the answer, corporate jargon.
- AI tells a reader would notice: contrast frames ("not just X, but Y"), stacked adjectives, padded lists of three where two items are real, the same thing called by rotating names, transitions that say nothing.
- A reader question from the brief that is answered vaguely.
- Anything that refers to how the article was produced.
- Items from the internal checklist (if one follows the draft) that matter for this reader. Turn those into \
fixes in your own words; skip any that do not fit this page. The checklist is for you, never for the page.
- A claim of first-party experience ("our clients", "we tested", "in our shop", a result of ours) that is not \
in the brief's first-party evidence. Tell the writer to cut it.
- Information gain. Mentally delete every sentence that comes from the brief's first-party evidence. If what is \
left could be published by any site ranking for this keyword, flag the two most generic sections and tell the \
writer how to tie them to the evidence, a worked example or a decision rule the ranking pages leave out.

Do not flag length, keyword placement, link counts or formatting: those are checked separately.
At most {max_fixes} fixes, most important first. If the draft is ready to publish, return no fixes.

Reply with one JSON object and nothing else:
{{"verdict": "approve" or "revise", "fixes": [{{"section": "<H2 heading or 'Introduction'>", \
"problem": "<what is wrong, one sentence>", "instruction": "<what the writer must do, one sentence>"}}]}}"""


def _parse_editor(raw: str, max_fixes: int) -> list[dict]:
    """Editor JSON -> fix list. Raises ValueError when the reply is not usable."""
    text = re.sub(r"^```(json)?\s*|\s*```\s*$", "", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in editor reply")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or not isinstance(data.get("fixes", []), list):
        raise ValueError("editor reply has the wrong shape")
    if str(data.get("verdict", "")).lower() == "approve":
        return []
    fixes = []
    for f in data.get("fixes", [])[:max_fixes]:
        if not isinstance(f, dict) or not str(f.get("instruction", "")).strip():
            continue
        section = str(f.get("section", "")).strip() or "Whole article"
        problem = str(f.get("problem", "")).strip()
        fixes.append({"code": "editor", "section": section,
                      "message": f'[{section}] {problem} {str(f["instruction"]).strip()}'.replace("  ", " ")})
    return fixes


class OpenRouterBackend(AnthropicBackend):
    name = "openrouter"

    def _call(self, config, system, user) -> str:
        cap = int(config.get("writer_max_tokens", 32000))
        try:
            try:
                return openrouter_chat(config, config["writer_model"], system, user, role="writer", max_tokens=cap)
            except TruncatedError as e:  # a cut-off reply is unusable; one retry with twice the room
                get_runtime(config).log("stage_error", stage="writer_truncated", error=str(e), retry_max_tokens=cap * 2)
                return openrouter_chat(config, config["writer_model"], system, user, role="writer",
                                       max_tokens=cap * 2)
        except ApiError as e:
            raise WriterError(str(e))

    def edit(self, article, brief, config) -> list[dict]:
        """Editor pass: returns fixes only. The editor's text never enters the article."""
        system = EDITOR_PROMPT.format(site=config["site_name"], niche=config["niche_description"],
                                      max_fixes=config["editor_max_fixes"])
        if lang.is_es():
            system += "\n" + lang.ES_EDITOR_RULES
        if config.get("writer_notes"):
            system += ("\n\nFacts and limits the writer was given (flag any claim about the business that goes "
                       "beyond them):\n" + str(config["writer_notes"]).strip())
        user = ("BRIEF:\n" + _brief_for_prompt(brief)
                + f'\n\nDRAFT:\nTITLE: {article["title"]}\nDESCRIPTION: {article["description"]}\n===\n'
                + article["body"])
        try:
            import aiaudit
            items = aiaudit.editor_items(article, brief, config)
        except Exception:  # noqa: BLE001 - the checklist is advisory; it never blocks the editor
            items = []
        if items:
            user += ("\n\nINTERNAL CHECKLIST (for you only; it is not part of the brief and must never be mentioned "
                     "in the article). This draft misses these AI-search checklist items:\n"
                     + "\n".join(f"- {i}" for i in items))
        raw = openrouter_chat(config, config["editor_model"], system, user, role="editor", json_mode=True)
        return _parse_editor(raw, config["editor_max_fixes"])


# =====================================================================
# Backend: deterministic template composer
# =====================================================================

MODELS = [
    ("the iceberg model", "Think of {kw} like an iceberg. The part you see first is small: a price, a promise, a "
     "headline. The part that decides how things turn out sits below the waterline. For this piece of the puzzle, "
     "the hidden mass is the detail nobody volunteers until you ask."),
    ("the 80/20 rule", "The 80/20 rule applies here. A small handful of factors drive most of the result with {kw}, "
     "and the rest is noise. Your job is to find the two or three that matter for your situation and spend your "
     "attention there, not evenly across everything."),
    ("inversion", "Try inversion, the mental model Charlie Munger made famous. Flip the question. Don't ask how to "
     "get {kw} right. Ask what would guarantee it goes badly, write that list down, and then avoid every item on it. "
     "It's a faster route than chasing perfection."),
    ("first-principles thinking", "This is a good place for first-principles thinking. Strip {kw} down to what is "
     "physically or financially true, ignore what everyone repeats, and rebuild from there. It works like taking an "
     "engine apart: once you've seen the pieces, the sales pitch stops being mysterious."),
    ("second-order thinking", "Use second-order thinking here: ask \"and then what?\" The first-order effect of a "
     "choice about {kw} is obvious. The second-order effect, what that choice sets in motion six months later, is "
     "where the real cost or payoff hides."),
    ("the pilot's checklist", "Borrow the pilot's checklist method. Pilots don't trust memory, even after ten "
     "thousand hours, because one skipped step is all it takes. Treat {kw} the same way: a short written list you "
     "tick off beats a confident guess every time."),
    ("the map is not the territory", "Remember the old rule that the map is not the territory. A quote, a spec "
     "sheet or a review is a map of {kw}. Your actual situation is the territory. When the two disagree, the "
     "territory wins, so check the real thing before you trust the paper."),
    ("opportunity cost", "Run the opportunity-cost test. Every dollar and hour you put into one option for {kw} is "
     "a dollar and hour you can't put somewhere else. It's like packing a small suitcase: adding one thing means "
     "leaving another out, so choose on purpose."),
    ("margin of safety", "Engineers use a principle called margin of safety. A bridge rated for ten tons is built "
     "to hold far more. Apply it to {kw}: leave room in your budget, your timeline and your expectations, because "
     "the plan that needs everything to go right usually doesn't."),
    ("Occam's razor", "Occam's razor helps here. When two explanations fit, the simpler one is usually right. If "
     "something about {kw} seems confusing, the plain answer, often about cost, time or who carries the risk, tends "
     "to beat the clever one."),
    ("the circle of competence", "Think in terms of a circle of competence. Inside the circle are the parts of {kw} "
     "you can judge yourself. Outside it are the parts where you need someone who does this daily. Knowing where "
     "your circle ends is like knowing where the deep end of the pool starts."),
    ("compounding", "Picture compound interest. Small decisions about {kw} don't stay small; they stack. A slightly "
     "better choice now keeps paying, and a slightly worse one keeps charging you, long after you've forgotten "
     "making it."),
]

OPENERS = [
    "Start with the simple version. \"{h2}\" sounds like one question, but it's really two or three stacked on "
    "top of each other. Pull them apart and each one gets easier to answer.",
    "Here's where most explanations of {kw} speed up. This is the part worth slowing down for, because the "
    "details in it decide what you actually end up with.",
    "Let's peel this back a layer. On the surface, this part of {kw} looks settled. Underneath, there are a few "
    "moving parts that change the answer depending on your situation.",
    "This is the section people skim and later wish they hadn't. So take it one step at a time, starting with "
    "what's true for almost everyone and then narrowing to what's true for you.",
    "You could answer this in one line, and plenty of pages do. The one-line answer is fine until your situation "
    "differs from the average, which it usually does.",
    "Ask ten people about this and you'll hear ten confident answers. The useful move is to work out why they "
    "differ, because the reason tells you which answer applies to you.",
    "Zoom in on this one. It's a small part of {kw} on paper, and a large part of whether you're happy with the "
    "result afterwards.",
    "Most of what you'll read on this repeats the same three points. They're true. They're also incomplete, so "
    "let's cover them quickly and then go where they stop.",
]

BODY = [
    "The short version: the right answer depends on a few specifics, and you already know most of them. Write "
    "down your constraints before you compare anything. Budget, timing and how long you need the result to last "
    "will rule out half the options before you've read a single review.",
    "In practice this plays out in small moments. A question you didn't ask. A line in the quote you didn't read. "
    "A detail you assumed was standard. None of them feel important on the day, and each of them can change the "
    "outcome with {kw}.",
    "Where do people get stuck? Usually at the point where two options look the same. They rarely are. Look at "
    "what's included, what's excluded and who is responsible if something goes wrong. The gap between two "
    "similar-looking offers almost always lives in one of those three places.",
    "A useful habit is to ask for the specific number, date or name behind every claim. \"Fast\" becomes \"within "
    "48 hours\". \"Affordable\" becomes an actual figure. \"Experienced\" becomes a count of similar jobs. Vague "
    "words cost nothing to say, so they tell you nothing.",
    "Timing matters more than it seems. The same decision about {kw} made in a hurry and made with a week to spare "
    "produces different results, mostly because a week lets you get a second opinion. If you can buy yourself "
    "time, do.",
    "Don't skip the boring part: writing it down. A two-line note of what was agreed, what it costs and when it "
    "happens saves more arguments than any amount of goodwill. Memory is generous to whoever is doing the "
    "remembering.",
    "Compare like with like. If one option covers three things and another covers five, you're not looking at two "
    "prices for the same thing. Line them up item by item first. The comparison usually gets obvious once the "
    "lists match.",
    "Watch for the default. Whatever happens when you make no choice is still a choice, and it's often the most "
    "expensive one. Ask what the default is here and whether it suits you, or whether it just suits whoever set it.",
    "A quick test: explain your plan for {kw} to a friend in three sentences. If you can't, there's a piece you "
    "don't understand yet. That's the piece to go and ask about before you commit to anything.",
    "Expect a trade-off. Cheaper usually means slower or narrower. Faster usually costs more. Longer-lasting "
    "usually means more up front. You get to pick which side of each trade you want, but you rarely get both.",
    "Small print isn't decoration. Guarantees, exclusions and renewal terms are where the real offer is described. "
    "Read those three things in full, even if you skim everything else. Ten minutes there is worth an hour of "
    "comparing headlines.",
    "Ask what happens afterwards. Who do you call if there's a problem in month three? What does a fix cost then? "
    "The answer to the after question tells you more about {kw} than the answer to the before question.",
    "Look for evidence you can check yourself. A recent example, a reference you can phone, a result you can see. "
    "One checkable fact beats five impressive claims, because only one of those can be wrong without you noticing.",
    "Give yourself a walk-away rule before you start. Decide the number, the date or the condition that means no. "
    "It's far easier to set that line calmly now than to find it in the middle of a conversation.",
]

EXPANSIONS = [
    "One more layer on this. The people who are happiest with how {kw} turned out tend to have done one unglamorous "
    "thing: they asked a second source the same question. Not because the first was wrong, but because two answers "
    "show you where the range is.",
    "Here's a concrete way to use this section. Take a sheet of paper and make two columns: what you know and what "
    "you're assuming. Most of the risk in {kw} sits in the second column. Turn one assumption into a known fact "
    "each day and the decision makes itself.",
    "It also helps to set a review point. Decide now when you'll look back and judge whether this worked: thirty "
    "days, ninety days, a year. A decision with a review date gets corrected. A decision without one just gets "
    "lived with.",
    "If you remember one thing from this part, make it this: ask the awkward question early. The cost of asking is "
    "a slightly uncomfortable minute. The cost of not asking shows up later, with interest.",
    "Consider the worst realistic case, not the worst imaginable one. What does a bad-but-normal outcome with {kw} "
    "look like, and could you live with it? If yes, you can move ahead with a clear head. If no, change the plan "
    "until the answer is yes.",
    "There's a pattern worth noticing. The loudest claims in this area are about speed and price, and the quietest "
    "are about what happens when something goes wrong. Pay the most attention to whoever talks plainly about the "
    "quiet part.",
    "A practical shortcut: find someone who went through this a year ago and ask what they'd do differently. A "
    "year is long enough for the consequences to show and short enough that they still remember the details.",
    "Put a number on it, even a rough one. \"This matters a lot\" doesn't help you choose. \"This is worth about "
    "this much to me\" does. Rough numbers beat precise feelings when two options are close.",
    "Keep a record as you go. Dates, names, figures, what was said. It takes two minutes each time and it turns a "
    "future disagreement into a quick lookup instead of a contest of memories.",
    "And if it still feels unclear, shrink the decision. You don't have to settle everything about {kw} today. "
    "Pick the next single step, take it, and let what you learn there shape the step after.",
]

EMPHASIS = ["That's the whole trick.", "Most people skip that step.", "It sounds small. It isn't.",
            "Write that down.", "This is where the money goes.", "Ask the question anyway.",
            "Slow is fine here.", "The details decide it."]

FAQ_GENERIC = {
    "cost": "It depends on scope, timing and who does the work, which is why quoted prices for {kw} vary so widely. "
            "Ask for an itemised figure, check what is excluded, and compare at least two quotes line by line "
            "before you treat any number as the going rate.",
    "time": "Timelines for {kw} depend on how prepared things are before work starts. Ask for a start date, an end "
            "date and what could push either one. A provider who can name the likely delays up front is usually "
            "the one who will hit the schedule.",
    "yesno": "Often yes, but it turns on the details of your setup, so treat any blanket answer with caution. "
             "Describe your exact situation to someone who handles {kw} every week and ask what would change their "
             "answer. The conditions they list are the real answer to this question.",
    "compare": "Neither side wins in every case. With {kw}, the better choice depends on what you are optimising "
               "for: lowest cost now, lowest cost over years, or least hassle. Rank those three for yourself first, "
               "and the comparison usually settles quickly.",
    "default": "The honest answer turns on your specifics, so start by writing those down: budget, timing and what "
               "a good result looks like for you. Then put this exact question to two separate sources on {kw} and "
               "compare what they say. Where the answers agree, you have your baseline.",
}


def _seeded(seq, seed: str, n: int = 0):
    h = int(hashlib.sha1(seed.encode()).hexdigest(), 16)
    return seq[(h + n) % len(seq)]


class _Rotor:
    """Deterministic, non-repeating picker over a template bank."""

    def __init__(self, bank, seed):
        start = int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(bank)
        self.items = bank[start:] + bank[:start]
        self.i = 0

    def next(self):
        item = self.items[self.i % len(self.items)]
        self.i += 1
        return item


def _cap(s: str) -> str:
    return smart_cap(s)


class TemplateBackend:
    name = "template"

    def _title_desc(self, brief, config=None):
        kw, Kw = brief["primary_keyword"], _cap(brief["primary_keyword"])
        options = {"informational": ["A Plain-English Guide", "Explained Simply", "The Basics", "Guide"],
                   "commercial": ["How to Compare Your Options", "How to Compare Options", "How to Choose", "Compared"],
                   "transactional": ["Costs, Choices and What to Check First", "Costs and What to Check",
                                     "Costs and Choices", "Costs"]}.get(brief["intent"],
                                                                        ["What to Know First", "What to Know", "Basics"])
        fits = [f"{Kw}: {o}" for o in options if len(f"{Kw}: {o}") <= 60]
        title = next((t for t in fits if len(t) >= 40), fits[0] if fits else Kw)
        if len(title) < 40:
            title = f"{title} ({content_year(config)} Guide)" if len(title) + 13 <= 60 else title
        desc = f"{Kw} explained step by step: what it involves, what it costs, how to choose, and the questions most guides skip."
        if len(desc) > 160:
            desc = f"{Kw}: what it involves, what it costs and what most guides skip."
        if len(desc) > 160:
            desc = desc[:157].rsplit(" ", 1)[0] + "..."
        if len(desc) < 50:
            desc += " A step-by-step guide with the questions to ask first."
        return title, desc

    def _evidence(self, brief, h2, used):
        """A real SERP snippet relevant to this section, attributed and linked."""
        want = set(tokens(h2)) - set(tokens(brief["primary_keyword"]))
        ext = {s["url"] for s in brief.get("external_sources", [])}
        best, score = None, 0
        for r in brief.get("serp_summary", {}).get("top_results", []):
            if r["url"] in used or r["url"] not in ext or word_count(r.get("snippet", "")) < 8:
                continue
            sc = len(want & set(tokens(r["snippet"]))) + 0.1
            if sc > score:
                best, score = r, sc
        if not best:
            return None
        used.add(best["url"])
        snippet = " ".join(normalize_quotes(best["snippet"]).replace('"', "'").split()[:14]).rstrip(" .,;:")
        return (f'For a second view, [{best["domain"]}]({best["url"]}) frames it this way: '
                f'"{snippet}..." Use that as a reference point, not a verdict.')

    def draft(self, brief, config) -> dict:
        kw = brief["primary_keyword"]
        Kw = _cap(kw)
        title, desc = self._title_desc(brief, config)
        models, openers = _Rotor(MODELS, kw + "m"), _Rotor(OPENERS, kw + "o")
        bodies, emph = _Rotor(BODY, kw + "b"), _Rotor(EMPHASIS, kw + "e")
        gaps = [g for g in brief["gaps_to_address"] if not g["section"].lower().startswith("frequently")]
        gap_names = [g["topic"].rstrip("?").lower() for g in gaps[:2]]
        used_evidence: set = set()
        out = []
        if config.get("include_h1_in_body", True):
            out.append(f"# {title}\n")

        teaser = " and ".join(gap_names) if gap_names else "the parts that usually get skipped"
        out.append(f"{Kw} looks simple from the outside. You search, you skim a few pages, and they all say "
                   f"roughly the same thing.\n")
        out.append(f"This guide covers the same ground, then keeps going into what those pages leave thin: {teaser}.\n")
        out.append("Take it one layer at a time.\n")

        majors = [s for s in brief["outline"] if not s.get("faq")]
        table = ["Here's the route this guide takes:\n", "| Section | What you'll take away |\n|---|---|"]
        for s in majors:
            note = "Covered in depth here because most ranking pages skip it" if s.get("gap") else \
                   "The essentials, with the reasoning behind them"
            table.append(f"| {s['h2']} | {note} |")
        table.append("")

        internal = list(brief.get("internal_link_targets") or [])
        dir_links = [t for t in internal if t["type"] != "article"]
        art_links = [t for t in internal if t["type"] == "article"]

        for idx, s in enumerate(majors):
            out.append(f"## {s['h2']}\n")
            out.append(openers.next().format(kw=kw, h2=s["h2"].rstrip("?")) + "\n")
            out.append(models.next()[1].format(kw=kw) + "\n")
            if idx == 0:
                out.extend(table)
            gap = next((g for g in brief["gaps_to_address"] if g["section"] == s["h2"]), None)
            if gap:
                out.append("Most pages that rank for this topic give this part a sentence or two, or leave it out "
                           "entirely. So it gets proper room here.\n")
            h3s = s["h3"] or []
            if h3s:
                for h in h3s:
                    out.append(f"### {h}\n")
                    out.append(bodies.next().format(kw=kw) + "\n")
                    out.append(bodies.next().format(kw=kw) + "\n")
            else:
                out.append(bodies.next().format(kw=kw) + "\n")
                out.append(emph.next() + "\n")
                out.append(bodies.next().format(kw=kw) + "\n")
                out.append("Before you move on, check three things:\n")
                out.append("- What exactly is included, in writing\n- What it costs in total, not just up front\n"
                           "- Who is responsible if it goes wrong\n")
                out.append(bodies.next().format(kw=kw) + "\n")
            ev = self._evidence(brief, s["h2"], used_evidence)
            if ev:
                out.append(ev + "\n")
            if idx == 0 and dir_links:
                parts = [f'[{t["anchor"]}]({t["url"]})' for t in dir_links[:3]]
                out.append("If you'd rather start from real names than theory, browse "
                           + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0])
                           + " and compare a few side by side.\n")
            if idx == 1 and art_links:
                parts = [f'[{t["anchor"]}]({t["url"]})' for t in art_links]
                out.append("Related reading that goes deeper on nearby questions: " + ", ".join(parts) + ".\n")
            out.append(emph.next() + "\n")

        faq = next((s for s in brief["outline"] if s.get("faq")), None)
        if faq:
            paa = {q["question"]: q for q in brief.get("serp_summary", {}).get("people_also_ask", [])}
            out.append(f"## {faq['h2']}\n")
            for q in faq["h3"]:
                out.append(f"### {q}\n")
                snip = normalize_quotes((paa.get(q) or {}).get("snippet") or "")
                ql = q.lower()
                kind = ("cost" if re.search(r"cost|price|pay|much", ql) else "time" if re.search(r"how long|time|take", ql)
                        else "yesno" if re.match(r"(do|does|is|are|can|should|will)\b", ql)
                        else "compare" if re.search(r"\b(vs|versus|or|better|difference|downside|which)\b", ql) else "default")
                ans = FAQ_GENERIC[kind].format(kw=kw)
                if word_count(snip) >= 12:
                    src = (paa[q].get("source_url") or "")
                    short = " ".join(snip.replace('"', "'").split()[:30]).rstrip(" .,;:")
                    cite = f" ([source]({src}))" if src.startswith("http") and "google." not in src else ""
                    ans = f'The commonly cited answer: "{short}..."{cite} ' + ans
                out.append(ans + "\n")

        remaining = [s for s in brief.get("external_sources", []) if s["url"] not in used_evidence]
        if remaining and len(used_evidence) < 2:
            out.insert(len(out) - (len(faq["h3"]) * 2 + 1 if faq else 0),
                       "Two more references worth a look while you compare: "
                       + " and ".join(f'[{s["domain"]}]({s["url"]})' for s in remaining[:2]) + ".\n")
        body = scrub_banned("\n".join(out)).strip() + "\n"
        return {"title": scrub_banned(title), "description": scrub_banned(desc), "body": body}

    def revise(self, article, fixes, brief, config) -> dict:
        kw = brief["primary_keyword"]
        body = article["body"]
        codes = {f["code"] for f in fixes}
        h1_line = ""
        m = re.match(r"^(# .*\n+)", body)
        if m:
            h1_line, body = m.group(1), body[m.end():]
        parts = re.split(r"(?m)^(?=## )", body)
        head, secs = parts[0], parts[1:]

        def is_major(sec):  # the evidence section stays item text only: never padded
            import evidence
            h = sec[3:].split("\n", 1)[0]
            return not NON_MAJOR.search(h) and not evidence.is_evidence_heading(h)

        models = _Rotor(MODELS, kw + "rev")
        for f in fixes:
            if f["code"] == "analogy_missing":
                for i, sec in enumerate(secs):
                    if sec.startswith(f"## {f['section']}\n"):
                        lines = sec.split("\n", 2)
                        secs[i] = lines[0] + "\n\n" + models.next()[1].format(kw=kw) + "\n" + "\n".join(lines[1:])
            if f["code"] in ("gap_not_covered", "gap_too_thin"):
                hit = next((i for i, sec in enumerate(secs) if sec.startswith(f"## {f['section']}\n")), None)
                filler = "\n".join(t.format(kw=kw) + "\n" for t in EXPANSIONS[:3])
                if hit is None:
                    pos = next((i for i, sec in enumerate(secs) if not is_major(sec)), len(secs))
                    secs.insert(pos, f"## {f['section']}\n\n{MODELS[0][1].format(kw=kw)}\n\n{filler}\n")
                else:
                    secs[hit] = secs[hit].rstrip() + "\n\n" + filler + "\n"
        if codes & {"evidence_missing", "evidence_section_missing", "evidence_image_missing",
                    "evidence_unsupported_number"}:
            import evidence
            secs = [sec for sec in secs if not evidence.is_evidence_heading(sec[3:].split("\n", 1)[0])]
            pos = next((i for i, sec in enumerate(secs) if not is_major(sec)), len(secs))
            secs.insert(pos, evidence.template_section(brief))
        import onpage
        if codes & {"key_takeaways_missing", "key_takeaways_shape", "key_takeaways_position"}:
            secs = [sec for sec in secs if not onpage.TAKEAWAYS_RE.search(sec[3:].split("\n", 1)[0])]
            secs.insert(0, onpage.template_takeaways(brief, "".join(secs)))
        for f in fixes:
            if f["code"] == "section_not_self_contained":
                for i, sec in enumerate(secs):
                    h = sec[3:].split("\n", 1)[0].strip()
                    if h in f.get("sections", []):
                        first, rest = sec.split("\n", 1)
                        secs[i] = f"{first}\n\n{h.rstrip('?.:')} is one of the parts of {kw} worth getting right.\n{rest}"
            if f["code"] == "fanout_missing":
                faq_i = next((i for i, sec in enumerate(secs) if is_faq_heading(sec[3:].split("\n", 1)[0])), None)
                qa = f"### {f['question']}\n\n{onpage.template_fanout_answer(f['bucket'], kw)}\n\n"
                if faq_i is None:
                    secs.append(f"## Frequently asked questions about {kw}\n\n" + qa)
                else:
                    secs[faq_i] = secs[faq_i].rstrip() + "\n\n" + qa
            if f["code"] == "best_for_missing":
                for i, sec in enumerate(secs):
                    for h in f.get("options", []):
                        secs[i] = re.sub(r"(?m)^(### " + re.escape(h) + r")\n",
                                         lambda m: m.group(1) + f"\n\nBest for: readers weighing {h} against the other options.\n",
                                         secs[i], count=1)
            if f["code"] == "cta_placement" and brief.get("on_page", {}).get("cta"):
                c = brief["on_page"]["cta"]
                link = f"\n\nReady to move? [{c['label']}]({c['url']}).\n\n"
                head = head.rstrip() + link
                majors = [i for i, sec in enumerate(secs) if is_major(sec)]
                for i in ({majors[len(majors) // 2], majors[-1]} if majors else set()):
                    secs[i] = secs[i].rstrip() + link
        if "word_count" in codes:
            need = brief["target_word_count"] + 40
            exp = _Rotor(EXPANSIONS + BODY, kw + "x")
            major_idx = [i for i, sec in enumerate(secs) if is_major(sec)] or list(range(len(secs)))
            guard = 0
            while word_count(plain_text(h1_line + head + "".join(secs))) < need and guard < 400:
                i = major_idx[guard % len(major_idx)]
                para = exp.next().format(kw=kw)
                if para not in secs[i] or guard >= len(EXPANSIONS + BODY) * len(major_idx):
                    secs[i] = secs[i].rstrip() + "\n\n" + para + "\n\n"
                guard += 1
        if "tone_no_emphasis" in codes:
            for i, sec in enumerate(secs[:4]):
                secs[i] = sec.rstrip() + "\n\n" + EMPHASIS[i % len(EMPHASIS)] + "\n\n"
        title, desc = article["title"], article["description"]
        if codes & {"keyword_title", "keyword_description", "description_length"}:
            title, desc = self._title_desc(brief, config)
        if "keyword_first_100" in codes:
            head = f"{_cap(kw)} is worth understanding before you spend anything on it.\n\n" + head
        body = scrub_banned(h1_line + head + "".join(secs)).strip() + "\n"
        if "back_reference" in codes:
            body = onpage.BACKREF_ANY.sub("", body)
        if "paragraph_too_long" in codes:
            body = _split_long_paragraphs(body, brief.get("on_page", {}).get("max_paragraph_words", 90))
        if "past_year" in codes:
            title, desc, body = (drop_past_years(t, content_year(config)) for t in (title, desc, body))
        return {"title": scrub_banned(title), "description": scrub_banned(desc), "body": body}


# =====================================================================
# Orchestration
# =====================================================================

def _split_long_paragraphs(body: str, limit: int) -> str:
    import onpage
    out = []
    for p in re.split(r"(\n\s*\n)", body):
        if p.strip() in onpage._paragraphs(p) and word_count(plain_text(p)) > limit:
            sents = _sentences(p.strip())
            chunk, chunks = [], []
            for sn in sents:
                if chunk and word_count(" ".join(chunk + [sn])) > limit * 0.6:
                    chunks.append(" ".join(chunk))
                    chunk = []
                chunk.append(sn)
            chunks.append(" ".join(chunk))
            p = "\n\n".join(chunks)
        out.append(p)
    return "".join(out)


def pick_backend(config):
    load_dotenv(ROOT / ".env")
    choice = config.get("writer_backend", "auto")
    if choice not in ("auto", "openrouter", "anthropic", "template"):
        raise WriterError(f"writer_backend must be auto, openrouter, anthropic or template (got '{choice}')")
    has_or = bool(optional_secret("OPENROUTER_API_KEY"))
    if choice == "openrouter" or (choice == "auto" and has_or):
        if not has_or:
            raise WriterError("writer_backend is 'openrouter' but OPENROUTER_API_KEY is not set in .env")
        return OpenRouterBackend()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if choice == "anthropic" or (choice == "auto" and has_key):
        if not has_key:
            raise WriterError("writer_backend is 'anthropic' but ANTHROPIC_API_KEY is not set in .env")
        return AnthropicBackend()
    if lang.is_es():
        raise WriterError("the template backend only writes English; set OPENROUTER_API_KEY or ANTHROPIC_API_KEY "
                          "for a Spanish site")
    return TemplateBackend()


def assemble(article: dict, brief: dict, config: dict, backend_name: str) -> str:
    if not config.get("include_h1_in_body", True):
        # The site layout renders the title; a writing model may still open the body with an H1.
        article = {**article, "body": re.sub(r"\A\s*#\s+[^\n]*\n+", "", article["body"])}
    faq = []
    _, _, sections = split_sections(article["body"])
    for s in sections:
        if is_faq_heading(s["h2"]):
            for q, a in re.findall(r"^###\s+(.+?)\n+(.*?)(?=^###\s|\Z)", s["text"], flags=re.M | re.S):
                faq.append({"question": q.strip(), "answer": " ".join(plain_text(a).split())})
    kw = brief["primary_keyword"]
    tags, seen = [], set()
    for t in [kw] + brief.get("secondary_keywords", [])[:3]:
        if t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t.lower())
    pub = brief.get("pubDate") or date.today().isoformat()
    fm = {
        "title": article["title"],
        "description": article["description"],
        "pubDate": date.fromisoformat(pub[:10]),
        "slug": brief["slug"],
        "tags": tags,
        "primaryKeyword": kw,
        "secondaryKeywords": brief.get("secondary_keywords", []),
        "schemaType": brief["schema_type"],
        "faq": faq,
    }
    import comparisons
    import onpage
    fm.update(comparisons.frontmatter(brief))
    fm.update(onpage.frontmatter(fm, article["body"], brief, config))
    if backend_name == "template":
        fm["draft"] = True  # template-composed copy: hold for an editorial pass before publishing
    if (brief.get("evidence") or {}).get("draft"):
        fm["draft"] = True  # no first-party evidence and the site asked for drafts: a person adds it first
    return build_frontmatter(fm) + "\n" + article["body"].strip() + "\n"


META_PROMPT = """You rewrite one search result's title and meta description so more searchers click it. \
The page itself does not change, so promise only what the page delivers.

Rules:
- Title: 40-60 characters. Start with the search query or its closest natural wording. Then the most specific \
reason to click this result: a number, a price range, a year, a named outcome or who it is for.
- Match what the ranking titles signal searchers want (prices, a year, a list, "near me", a comparison), \
without copying any of them.
- Description: 120-155 characters, contains the query, says what the reader gets and one concrete detail \
from the page.
- No clickbait, no ALL CAPS, no emoji, no dashes, no "Ultimate guide", no exclamation marks.
- If a year helps, use {year}. Never name an earlier year, even when ranking titles do.
- Output exactly:
TITLE: <title>
DESCRIPTION: <description>"""


def _meta_fallback(old: dict, query: str, titles: list[str], year: int | None = None) -> dict:
    """Deterministic title when no LLM is available: query first, then the signal most ranking titles carry."""
    year = year or content_year()
    q = smart_cap(query)
    joined = " ".join(titles).lower()
    tails = []
    if re.search(r"\b20\d\d\b", joined):
        tails.append(f"({year})")
    if re.search(r"\$|\bcost|\bprice", joined):
        tails.append("Costs and What to Check")
    tails += ["What to Know Before You Pay", "A Plain-English Guide", "Guide"]
    title = q
    for tail in tails:
        cand = f"{q}: {tail}" if not tail.startswith("(") else f"{q} {tail}"
        if 40 <= len(cand) <= 60:
            title = cand
            break
    first = " ".join(plain_text(old.get("body", "")).split()[:40])
    lead = f"{q}: {first}"
    lead = (lead[:152].rsplit(" ", 1)[0].rstrip(",.;:") + ".") if len(lead) > 152 else lead
    options = [old.get("description") or "", lead,
               f"{q}: what it costs, what changes the price, and the questions to ask before you hire anyone or pay a deposit.",
               f"{q}: what it involves, what it costs and what to check first, explained step by step with real examples."]
    desc = next((d for d in options if 120 <= len(d) <= 160 and query.lower() in d.lower()), options[2][:158])
    return {"title": title, "description": desc}


def rewrite_meta(config, old: dict, query: str, primary_keyword: str, serp_titles: list[str], avoid=()) -> dict:
    """New {title, description} for a page with impressions but few clicks. Up to 3 LLM attempts, then a fallback."""
    import refresh
    backend = pick_backend(config)
    if backend.name == "template":
        return _meta_fallback(old, query, serp_titles, content_year(config))
    lead = " ".join(plain_text(old["body"]).split()[:250])
    user = (f"Search query: {query}\nPage's main keyword: {primary_keyword}\n"
            f"Current title: {old['title']}\nCurrent description: {old['description']}\n"
            + ("Titles to avoid (other pages use them): " + "; ".join(a for a in avoid if a) + "\n" if avoid else "")
            + "Titles ranking now:\n" + "\n".join(f"- {t}" for t in serp_titles[:10])
            + f"\n\nStart of the page:\n{lead}")
    feedback = ""
    for _ in range(3):
        try:
            got = _parse_llm(backend._call(config, META_PROMPT.replace("{year}", str(content_year(config))), user + feedback),
                             fallback={"title": old["title"], "description": old["description"], "body": ""})
        except (ApiError, ValueError) as e:
            get_runtime(config).log("stage_error", stage="retitle", error=str(e)[:200])
            break
        got = {"title": got["title"].strip().strip('"'), "description": got["description"].strip().strip('"')}
        problems = refresh.title_ok(got["title"], query) + refresh.desc_ok(got["description"], query)
        if old_years := past_years(got["title"] + " " + got["description"], content_year(config)):
            problems.append(f"name no year before {content_year(config)} (found {', '.join(old_years)})")
        if got["title"] in avoid:
            problems.append("the title must differ from the titles to avoid")
        if not problems:
            return got
        feedback = "\n\nYour last answer broke these rules; fix them: " + "; ".join(problems)
    return _meta_fallback(old, query, serp_titles, content_year(config))


def clean(article: dict) -> dict:
    """Banned-phrase swaps and the mechanical slop fixes, applied after every writer pass."""
    import slop
    return {k: slop.scrub(scrub_banned(v)) if isinstance(v, str) else v for k, v in article.items()}


def blocking(fixes: list[dict]) -> list[dict]:
    """Fixes that fail the gate (advisory ones are sent to the writer but never block the article)."""
    return [f for f in fixes if not f.get("advisory")]


def generate_article(brief: dict, config: dict, base: dict | None = None) -> str:
    """Draft -> critique -> revise (repeat) -> full Markdown with frontmatter.

    With `base` (an existing {title, description, body}), the first step revises that article against the
    brief instead of drafting a new one; this is how refresh.py updates a published page.
    Raises WriterError if the final critique still lists required fixes.
    """
    rt = get_runtime(config)
    backend = pick_backend(config)
    kw = brief["primary_keyword"]
    import evidence
    ev = evidence.attach(brief, config, refreshing=base is not None)
    import onpage
    onpage.attach(brief, config)
    import aivis
    aivis.attach(brief, config)
    rt.log("stage", stage="write", keyword=kw, backend=backend.name, refresh=base is not None,
           evidence=[it["id"] for it in ev["items"]], evidence_missing=ev["missing"])

    if base is not None:
        update = {"code": "refresh", "message": "Update this published article against the brief and the refresh rules "
                                                "at the end of it. Keep what is still accurate; fix what is out of date."}
        article = backend.revise(base, [update] + critique(base, brief, config), brief, config)
    else:
        article = backend.draft(brief, config)
    article = clean(article)

    # Writer <-> editor alternation (LLM backends that have an editor). Each round the editor
    # reviews the current draft and the writer applies the editor's fixes plus the rule fixes.
    if hasattr(backend, "edit"):
        for rnd in range(1, config.get("editor_rounds", 2) + 1):
            try:
                notes = backend.edit(article, brief, config)
            except (ApiError, ValueError) as e:  # an unusable editor reply never blocks the article
                rt.log("stage_error", stage="editor", keyword=kw, round=rnd, error=f"{type(e).__name__}: {e}")
                rt.editor_skipped.append(kw)
                break
            rule_fixes = critique(article, brief, config)
            rt.log("editor_review", keyword=kw, round=rnd, editor_fixes=[n["message"] for n in notes],
                   rule_fixes=[f["code"] for f in rule_fixes])
            if not notes:
                break  # editor approved; the rule-based gate below handles anything left
            article = backend.revise(article, rule_fixes + notes, brief, config)
            article = clean(article)

    fixes = critique(article, brief, config)           # the critique pass always runs
    rt.log("critique", keyword=kw, round=0, fixes=[f["code"] for f in fixes], detail=fixes)
    for rnd in range(1, config.get("max_revision_passes", 3) + 1):
        if not blocking(fixes):
            break
        article = backend.revise(article, fixes, brief, config)
        article = clean(article)
        fixes = critique(article, brief, config)
        rt.log("critique", keyword=kw, round=rnd, fixes=[f["code"] for f in fixes], detail=fixes)
    if advisory := [f["code"] for f in fixes if f.get("advisory")]:
        rt.log("advisory_left", keyword=kw, codes=advisory)
    fixes = blocking(fixes)
    if fixes:
        raise WriterError(f"'{kw}' still has {len(fixes)} required fix(es) after revision: "
                          + "; ".join(f["code"] for f in fixes), fixes)
    md = assemble(article, brief, config, backend.name)
    rt.log("stage", stage="write_done", keyword=kw, words=word_count(plain_text(article["body"])),
           target=brief["target_word_count"])
    return md
