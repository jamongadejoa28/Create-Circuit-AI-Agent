# 데이터시트

`docs/working-rules.md` **규칙 3**: 값과 규칙에는 **이 저장소 안에 있는** 출처가 있어야 하고,
PyMuPDF로 읽어 확인할 수 있어야 합니다. 검증 불가능한 인용은 없느니만 못합니다 —
아무도 확인하지 않은 판정을 내리게 하니까요.

이 폴더의 모든 PDF는 **제조사 공식 배포본**입니다. st.com `/resource/en/...` 는
이 환경에서 타임아웃되므로, ST 문서는 유통사(Farnell)나 기존에 공개된 동일 문서
사본에서 받았고, 1페이지 문서번호·개정판으로 제조사 문서임을 확인했습니다.
받은 날짜: ESP32·G4는 **2026-08-11**, 나머지(555/LM386/MCP6001/TMP100/AMS1117/F103/H743/AN2867)는
**2026-08-17**, W25Q32JV는 **2026-08-18**. 전부 PyMuPDF로 열리는 것을 확인했습니다.

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
| `stm32g4_RM0440_rev9.pdf` | RM0440 Rev 9 | 2140 | [st.com](https://www.st.com/resource/en/reference_manual/rm0440-stm32g4-series-advanced-armbased-32bit-mcus-stmicroelectronics.pdf) |
| `esp32_datasheet_en.pdf` | ESP32 Series, Version 5.3 | 78 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32_datasheet_en.pdf) |
| `esp32-s2_datasheet_en.pdf` | ESP32-S2 Series, Version 1.9 | 65 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-s2_datasheet_en.pdf) |
| `esp32-s3_datasheet_en.pdf` | ESP32-S3 Series, Version 2.2 | 87 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-s3_datasheet_en.pdf) |
| `esp32-c3_datasheet_en.pdf` | ESP32-C3 Series, Version 2.4 | 76 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-c3_datasheet_en.pdf) |
| `esp32-c6_datasheet_en.pdf` | ESP32-C6 Series, Version 1.5 | 86 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-c6_datasheet_en.pdf) |
| `esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.pdf` | ESP32-WROOM-32E/32UE, Version 2.1 | 52 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.pdf) |
| `esp32-s3-wroom-1_wroom-1u_datasheet_en.pdf` | ESP32-S3-WROOM-1/1U, Version 1.8 | 53 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-s3-wroom-1_wroom-1u_datasheet_en.pdf) |
| `esp32-c3-mini-1_datasheet_en.pdf` | ESP32-C3-MINI-1/1U, Version 2.2 | 48 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-c3-mini-1_datasheet_en.pdf) |
| `esp32-c3-wroom-02_datasheet_en.pdf` | ESP32-C3-WROOM-02/02U, Version 1.7 | 47 | [espressif.com](https://www.espressif.com/sites/default/files/documentation/esp32-c3-wroom-02_datasheet_en.pdf) |
| `ne555_SLFS022K.pdf` | SLFS022K (NE555 family) | 39 | [ti.com](https://www.ti.com/lit/ds/symlink/ne555.pdf) |
| `lm386_SNAS545D.pdf` | SNAS545D | 34 | [ti.com](https://www.ti.com/lit/ds/symlink/lm386.pdf) |
| `mcp6001_DS20001733L.pdf` | DS20001733L | 50 | [microchip.com](https://ww1.microchip.com/downloads/en/DeviceDoc/MCP6001-1R-1U-2-4-1-MHz-Low-Power-Op-Amp-DS20001733L.pdf) |
| `tmp100_SBOS231I.pdf` | SBOS231I | 31 | [ti.com](https://www.ti.com/lit/ds/symlink/tmp100.pdf) |
| `ams1117_ds1117.pdf` | AMS1117 ds1117 | 8 | [advanced-monolithic.com](http://www.advanced-monolithic.com/pdf/ds1117.pdf) |
| `stm32f103x8_DS5319.pdf` | DS5319 Rev 18 | 116 | [st.com](https://www.st.com/resource/en/datasheet/stm32f103c8.pdf) — bytes via [Farnell 3770728](https://www.farnell.com/datasheets/3770728.pdf) |
| `stm32h743_DS12110.pdf` | DS12110 Rev 10 | 357 | [st.com](https://www.st.com/resource/en/datasheet/stm32h743vi.pdf) — bytes via [Farnell 4001001](https://www.farnell.com/datasheets/4001001.pdf) |
| `an2867_oscillator.pdf` | AN2867 DocID15287 Rev 9 | 41 | [st.com](https://www.st.com/resource/en/application_note/an2867-oscillator-design-guide-for-stm8afals-stm32-mcus-and-mpus-stmicroelectronics.pdf) — bytes via public ST copy at [hands.com](https://hands.com/~lkcl/eoma/laptop_15in/CD00221665.pdf). Latest on st.com is Rev 24; CL formula is cited from this Rev 9 copy. |
| `w25q32jv_revG.pdf` | W25Q32JV Revision G | 80 | [winbond.com](https://www.winbond.com/hq/support/documentation/downloadV2022.jsp?__locale=en_TW&xmlPath=/support/resources/.content/item/DA00-W25Q32JV.html&level=1) — 제조사 페이지는 로그인. bytes via [Octopart 113071625](https://datasheet.octopart.com/W25Q32JVSSIQ-Winbond-datasheet-113071625.pdf). 1페이지 Publication Release Date March 27, 2018 / Revision G. |

칩 데이터시트와 **모듈** 데이터시트를 모두 둔 이유: 실제로 보드에 올라가는 것은 모듈인 경우가
많고, 모듈의 전원·안테나·스트래핑 핀 요구사항은 칩 문서에 없습니다.

Keil에서 받은 995쪽 PDF는 DS5319가 아니라 **RM0008**이었습니다. 잘못된 파일은 넣지 않습니다.
`tests/tools/check_datasheets.py`가 폴더 전체를 열어 확인하고, 같은 문서가 두 이름으로
들어오면 잡아냅니다.

## 정정: 대체 기능(AF) 매핑표는 RM0440이 아니라 데이터시트에 있습니다

제가 RM0440을 1순위로 요청드리면서 "AF 표가 거기 있다"고 했는데 **틀렸습니다.**
RM0440에 있는 것은 AF **레지스터**(GPIOx_AFRL/AFRH) 설명뿐이고, 어느 핀이 어느 기능을
지원하는지 적은 **매핑표는 DS12288 Table 13**, 이 저장소의
`stm32g474xB-xC-xE_DS12288_rev6.pdf` **pdf 페이지 인덱스 72~78**에 있습니다.

```
PB6 ... TIM4_CH1 ... TIM8_CH1 ... USART1_TX ... FDCAN2_TX ...
```

RM0440도 버리지 않았습니다 — 페리페럴 동작·클럭 트리·부팅 구성의 근거가 필요할 때
쓸 유일한 문서입니다.
