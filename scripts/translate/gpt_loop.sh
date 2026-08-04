#!/usr/bin/env bash
# GPT 초록 번역 자동 재시도 루프.
# gpt_translate_desc.py 는 스로틀(한도) 감지 시 깔끔히 중단·종료한다.
# 이 루프는 종료될 때마다 잠시 쉬었다(한도 회복) 다시 실행해 다음 버스트를 긁어온다.
# 남은 초록(any-model 미완료)이 0이면 종료.
cd /data_raid/ruci_workspace/frwaler_job
PY=.venv-embed/bin/python
LOG=scripts/translate/gpt_translate.log
COOLDOWN="${1:-90}"   # 버스트 사이 대기(초)

while true; do
  remaining=$($PY scripts/translate/gpt_translate_desc.py --count --unsafe-langs 2>/dev/null \
              | grep -oE '남음 [0-9,]+' | grep -oE '[0-9,]+' | tr -d ,)
  if [ -z "$remaining" ] || [ "$remaining" -le 0 ]; then
    echo "$(date '+%F %T')  [loop] 저자원 초록 0 → 루프 종료" >> "$LOG"
    break
  fi
  echo "$(date '+%F %T')  [loop] 저자원 남음 ${remaining} → GPT 버스트 실행" >> "$LOG"
  $PY scripts/translate/gpt_translate_desc.py --workers 4 --unsafe-langs
  sleep "$COOLDOWN"
done
