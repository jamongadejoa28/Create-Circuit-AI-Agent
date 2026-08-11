# 데이터시트

`docs/working-rules.md` **규칙 3**: 값과 규칙에는 **이 저장소 안에 있는** 출처가 있어야 하고,
PyMuPDF로 읽어 확인할 수 있어야 합니다. 검증 불가능한 인용은 없느니만 못합니다 —
아무도 확인하지 않은 판정을 내리게 하니까요.

이 폴더의 모든 PDF는 **제조사 공식 배포본**이고, 아래 표의 URL에서 그대로 받은 것입니다.
받은 날짜: **2026-08-11**. 전부 PyMuPDF로 열리는 것을 확인했습니다.

## 코드에서 인용하는 법

```python
# Source: STM32G474xB/xC/xE datasheet DS12288 Rev 6
# (data/datasheets/stm32g474xB-xC-xE_DS12288_rev6.pdf), section 5.1.6,
# Figure 16, pdf page index 80 / printed 81.
```

파일명·문서 개정판·**pdf 페이지 인덱스**까지 적습니다. 인쇄 페이지 번호와 PDF 인덱스는
다르므로 둘 다 적어야 다음 사람이 찾을 수 있습니다.

## 보유 문서

| 파일 | 문서 | 쪽 | 공식 출처 |
|---|---|---|---|
| `stm32g474xB-xC-xE_DS12288_rev6.pdf` | DS12288 Rev 6 | 236 | [st.com](https://www.st.com/resource/en/datasheet/stm32g474ve.pdf) |
| `esp32_datasheet_en.pdf` | ESP32 Series, Version 5.3 | 78 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32_datasheet_en.pdf) |
| `esp32-s2_datasheet_en.pdf` | ESP32-S2 Series, Version 1.9 | 65 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-s2_datasheet_en.pdf) |
| `esp32-s3_datasheet_en.pdf` | ESP32-S3 Series, Version 2.2 | 87 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-s3_datasheet_en.pdf) |
| `esp32-c3_datasheet_en.pdf` | ESP32-C3 Series, Version 2.4 | 76 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-c3_datasheet_en.pdf) |
| `esp32-c6_datasheet_en.pdf` | ESP32-C6 Series, Version 1.5 | 86 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-c6_datasheet_en.pdf) |
| `esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.pdf` | ESP32-WROOM-32E/32UE, Version 2.1 | 52 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.pdf) |
| `esp32-s3-wroom-1_wroom-1u_datasheet_en.pdf` | ESP32-S3-WROOM-1/1U, Version 1.8 | 53 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-s3-wroom-1_wroom-1u_datasheet_en.pdf) |
| `esp32-c3-mini-1_datasheet_en.pdf` | ESP32-C3-MINI-1/1U, Version 2.2 | 48 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-c3-mini-1_datasheet_en.pdf) |
| `esp32-c3-wroom-02_datasheet_en.pdf` | ESP32-C3-WROOM-02/02U, Version 1.7 | 47 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-c3-wroom-02_datasheet_en.pdf) |

칩 데이터시트와 **모듈** 데이터시트를 모두 둔 이유: 실제로 보드에 올라가는 것은 모듈인 경우가
많고, 모듈의 전원·안테나·스트래핑 핀 요구사항은 칩 문서에 없습니다.

## 아직 없는 것 — 직접 받아주셔야 합니다

**st.com은 이 환경에서 자동 다운로드를 차단합니다.** 사이트 루트는 301을 주지만
`/resource/en/...` PDF 경로는 5분을 기다려도 0바이트입니다(봇 차단). 비공식 미러로
대체하지 않았습니다 — 공식 배포본이 아니면 인용의 근거가 되지 못합니다.

브라우저로 받아서 이 폴더에 넣어주시면 됩니다:

| 문서 | 왜 필요한가 | URL |
|---|---|---|
| **RM0440** STM32G4 시리즈 레퍼런스 매뉴얼 | 대체 기능(AF) 표 — `USART1_TX`가 몇 번 핀인지 아는 유일한 근거. 지금 MCU 핀을 **이름**으로 연결하려다 실패하는 문제의 해답 | [st.com](https://www.st.com/resource/en/reference_manual/rm0440-stm32g4-series-advanced-armbased-32bit-mcus-stmicroelectronics.pdf) |
| STM32F103 데이터시트 | 가장 흔한 입문 MCU | [st.com](https://www.st.com/resource/en/datasheet/stm32f103c8.pdf) |
| STM32H743 데이터시트 | 고성능 대안 | [st.com](https://www.st.com/resource/en/datasheet/stm32h743vi.pdf) |
| AN2867 크리스탈 발진기 설계 | 크리스탈 부하 커패시터 값의 근거 | [st.com](https://www.st.com/resource/en/application_note/an2867-oscillator-design-guide-for-stm8afals-stm32-mcus-and-mpus-stmicroelectronics.pdf) |

파일을 넣으신 뒤 이 표에 파일명·개정판·쪽수를 추가해 주시면 됩니다.
`scripts/check_datasheets.py`가 폴더 전체를 열어 확인합니다.
