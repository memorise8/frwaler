"""Source date roles on Académie PDF covers; never borrow a portal or event date."""
import re

_MONTH = r'(?:janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre)'
_DAY = rf'\d{{1,2}}(?:er)?\s+{_MONTH}\s+\d{{4}}'
_PARTIAL = rf'{_MONTH}\s+\d{{4}}'
_ENGLISH = r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}'
_WEEKDAY = r'(?:lundi|mardi|mard\s+i|mercredi|jeudi|vendredi|samedi|dimanche)'


def publication_date_evidence(text):
    # A later page can contain a cited report, ceremony, or another publication.
    first_page = (text or '').split('\f', 1)[0]
    lines = [' '.join(line.split()) for line in first_page.splitlines() if line.strip()]
    header = ' '.join(lines[:12])
    press = bool(re.search(r'communiqu[ée]\s+de\s+presse', header, re.I))
    dossier = bool(re.search(r'dossier\s+de\s+presse', header, re.I))
    candidates, other_dates = [], []

    def add(raw, role, line):
        value = dict(raw=raw, role=role, source_line=line)
        if value not in candidates:
            candidates.append(value)

    for index, line in enumerate(lines[:12]):
        inline = re.match(rf'^(Paris,\s+le\s+{_DAY})\s*[–—-]\s+', line, re.I)
        if inline:
            add(inline[1], 'explicit_place_dateline', line)
            continue
        labelled = re.fullmatch(rf'(?:Paris,\s+)?le\s+{_DAY}', line, re.I)
        if labelled and (press or dossier):
            add(line, 'press_dateline', line)
            continue
        weekday = re.fullmatch(rf'{_WEEKDAY}\s+{_DAY}', line, re.I)
        if weekday:
            if press and not dossier:
                add(line, 'press_dateline', line)
            else:
                other_dates.append(dict(raw=line, role='event_or_cover_date_unconfirmed_as_publication'))
            continue
        if re.fullmatch(_DAY, line, re.I) and index < 5:
            # Reviewed bare document header dates and pre-heading press dates.
            add(line, 'document_header_date', line)
        elif re.fullmatch(_ENGLISH, line, re.I) and index < 5:
            add(line, 'document_header_date', line)
        elif press and index < 4 and re.fullmatch(_PARTIAL, line, re.I):
            add(line, 'press_issue_month', line)
        elif index < 6 and re.search(r'\b[ÉE]pisode\s+\d+', ' '.join(lines[:index+1]), re.I):
            month = re.search(rf'\(({_PARTIAL})\)\s*$', line, re.I)
            if month:
                add(month[1], 'episode_issue_month', line)

    # The reviewed multi-page opinion identifies its date in a repeated footer.
    if len(lines) >= 2 and re.fullmatch(_DAY, lines[-1], re.I):
        if re.match(r'Avis de l[’\']Académie des sciences sur\b', lines[-2], re.I):
            add(lines[-1], 'opinion_footer_date', lines[-2]+'\n'+lines[-1])

    unique = {v['raw'] for v in candidates}
    return dict(
        published_date_raw=next(iter(unique)) if len(unique) == 1 else None,
        state='identified' if len(unique) == 1 else 'ambiguous' if unique else 'not_identified',
        candidates=candidates,
        other_cover_dates=other_dates,
        portal_date_used_as_publication=False,
    )
