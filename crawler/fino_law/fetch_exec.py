from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

_API = "https://taxlaw.nts.go.kr/action.do"
_DL = "https://taxlaw.nts.go.kr/downloadFile.do"
_REFERER = "https://taxlaw.nts.go.kr/st/USESTE001M.do"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def _post(action_id: str, param: dict, delay: float) -> str:
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "60", "-X", "POST",
        "-H", "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
        "-H", f"User-Agent: {_UA}", "-H", "Origin: https://taxlaw.nts.go.kr",
        "-H", f"Referer: {_REFERER}", "-H", "X-Requested-With: XMLHttpRequest",
        "--data-urlencode", f"paramData={json.dumps(param, ensure_ascii=False)}",
        "-d", f"actionId={action_id}", _API,
    ]
    for attempt in range(3):
        if delay > 0:
            time.sleep(delay)
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=65).stdout
        if out.strip():
            return out
        time.sleep(1 + attempt)
    raise RuntimeError(f"taxlaw empty response: {action_id}")


def fetch_article_list(ntst_bsc_id: str, rgt_year: str, delay: float = 0.4) -> dict:
    return json.loads(_post("ASISTE001MR02", {"ntstBscId": ntst_bsc_id, "rgtYr": rgt_year}, delay))


def latest_publication(ntst_bsc_id: str, ntst_plcn_bk_id: str, delay: float = 0.4) -> dict | None:
    """ASISTE001MR03 → 최신연도 발간본 {rgtYr, fleId, fleSn}."""
    data = json.loads(_post("ASISTE001MR03",
                            {"ntstBscId": ntst_bsc_id, "ntstPlcnBkId": ntst_plcn_bk_id, "rgtYr": ""}, delay))
    rows = data.get("data", {}).get("ASISTE001MR03", {}).get("exeBaseDVOList", [])
    rows = [r for r in rows if r.get("rgtYr") and r.get("fleId")]
    if not rows:
        return None
    best = max(rows, key=lambda r: str(r.get("rgtYr")))
    return {"rgt_year": str(best["rgtYr"]), "fle_id": str(best["fleId"]), "fle_sn": str(best.get("fleSn", "0"))}


def download_pdf(fle_id: str, fle_sn: str, dest: Path, delay: float = 0.4) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if delay > 0:
        time.sleep(delay)
    subprocess.run(
        ["curl", "-skL", "--tls-max", "1.3", "--max-time", "120",
         "-H", f"User-Agent: {_UA}", "-H", f"Referer: {_REFERER}",
         "-o", str(dest), f"{_DL}?fleId={fle_id}&fleSn={fle_sn}"],
        check=True, timeout=130,
    )
    return dest
