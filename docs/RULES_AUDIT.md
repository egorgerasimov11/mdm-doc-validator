# Аудит правил — пакет решений для оператора (2026-07-07)

Это материал для **Гейта 1**: ты проходишь панель `/ui/rules/approve` и решаешь
развилки ниже. Ничего из этого файла не применяется без твоего решения.
Статистика «хиты» = реальные срабатывания на живых прогонах 18-документного корпуса.

## Как читать severity vs verdict (ЛЕГЕНДА — важно)
- `severity` — насколько сигнал серьёзен (что показать оператору первым).
- `verdict_effect` — что правило делает с вердиктом. **CRITICAL + NEED_MANUAL_REVIEW
  означает «высокий сигнал → отдать человеку», а НЕ «авто-отказ».** Настоящих
  авто-REJECT в системе три: BNK-001 (инвойс), BNK-002 (email), BNK-003 (редактируемый файл).

## Развилка №1 — BNK-002 (email как подтверждение) — РЕШИТЬ ДО слепого прогона
Сейчас: `email → CRITICAL REJECT (always)`. Твой скилл mdm-banking-checker STEP 4
допускает vendor-email с реквизитами для **HCP-вендоров** (exception-only). Валидатор
не видит HCP-контекста запроса → безусловный REJECT ложно рубит легитимный HCP-кейс.
В слепом наборе — ровно 4 таких .eml (REDOX, CAMPOVERDE, Snaco, Stichting).

| Вариант | Поведение | Плюс | Минус |
|---|---|---|---|
| **A (рекомендую)** | `verdict: NEED_MANUAL_REVIEW`, сообщение «email — exception-only (HCP/Finance approval): подтверди контекст» | соответствует скиллу и принципу «NMR вместо угадывания»; ложных REJECT нет | email больше не блокируется автоматически |
| B | Оставить REJECT | строго для обычных вендоров | ложный REJECT для HCP; противоречит скиллу |
| C | Оставить REJECT + править скилл (email всегда запрещён) | единообразие | меняет твой рабочий процесс HCP |

## Развилка №2 — новое правило BNK-027 (спец-страны, ручной ввод банка)
Скилл STEP 6: для **AE / TN / IQ / EG** — «Manual bank entry required. Do NOT use
Generate Bank Details». В YAML этого нет. Предлагаю NOTE-правило (не двигает вердикт):
```yaml
  - id: BNK-027
    name: manual_bank_entry_country
    applies_to: [bank_letter, bank_statement, supplier_letterhead, bank_screenshot,
                 voided_check, ap_document, payment_instructions, other]
    when: {check: field_in, field: bank_country, args: {values: [AE, TN, IQ, EG]}}
    severity: NOTE
    verdict_effect: null
    message: "Bank country {value}: manual bank entry required in SAP — do NOT use Generate Bank Details."
    message_ru: "Страна банка {value}: в SAP банк вводится вручную — НЕ использовать Generate Bank Details."
```
(нужен новый предикат `field_in` — логика, портируется и в ABAP; заведём через
propose→approve флоу — заодно живой e2e-тест этого механизма).
Решение: **добавить / не добавлять**.

## Развилка №3 — подтверждения «как задумано» (галочки)
- W-9 никогда не даёт hard-REJECT (все проблемы → NMR/WARNING, решает человек) — **ок?**
- BNK-010/011 (SWIFT/IBAN форма): severity CRITICAL, вердикт NMR (не блок) — **ок?**
- holder≠vendor и bank-country≠vendor-country проверяются НЕ движком правил, а слоем
  сверки с SAP/формой (SAP-000..008) — задокументировано, — **ок?**

## Таблица всех правил: рекомендация + tier (провенансность)
`tier: corp` = защитимо политикой (едет в корп-версию v1) · `experimental` = новый
подтип, обкатывается · `learned` = выученная эвристика (в корп v1 НЕ едет).

| Правило | Суть | Verdict | Хиты | Рекомендация | tier / source |
|---|---|---|---|---|---|
| BNK-001 | инвойс ≠ банк-подтверждение | REJECT | 0* | **Approve** | corp / policy |
| BNK-002 | email ≠ подтверждение | REJECT | 0* | **Развилка №1** | corp / skill |
| BNK-003 | редактируемый файл | REJECT | 0* | **Approve** | corp / policy |
| BNK-004 | payment_instructions ≠ инвойс | WARNING | 0* | **Approve** | experimental / operator |
| BNK-005 | AP/HCP форма — self-certified | NOTE | 1 | **Approve** | experimental / skill |
| BNK-006 | в выписке нет SWIFT — норма | NOTE | 0 | **Approve** | experimental / operator |
| BNK-010 | форма SWIFT/BIC | NMR | 0* | **Approve** | corp / skill |
| BNK-011 | IBAN длина/чексумма/страна | NMR | 1 | **Approve** | corp / skill |
| BNK-020 | документ старше 2 лет | NOTE | 1 | **Approve** | corp / skill |
| BNK-021 | письмо без подписи и свидетельств | WARNING | 2 | **Approve** | corp / skill |
| BNK-022 | скриншот обрезан | NMR | 0 | **Approve** | experimental / operator |
| BNK-023 | нет имени держателя | NMR | 2 | **Approve** | corp / skill |
| BNK-024 | нет банковских идентификаторов | NMR | 0 | **Approve** | corp / skill |
| BNK-025 | не читается имя банка | WARNING | 0 | **Approve** | corp / skill |
| BNK-026 | typed officer block / system-issued | NOTE | 2 | **Approve** | learned / operator |
| W9-001 | нет Line 1 | NMR | 0 | **Approve** | corp / skill |
| W9-002 | TIN не читается | NMR | 0 | **Approve** | corp / skill |
| W9-003 | нет классификации | NMR | 0 | **Approve** | corp / skill |
| W9-010 | EIN/SSN ≠ 9 цифр | NMR | 0 | **Approve** | corp / policy |
| W9-011 | тип TIN vs классификация | NMR | 0 | **Approve** (review-only) | learned / skill |
| W9-012 | Individual+бизнес-имя+EIN | NMR | 0 | **Approve** (review-only; из DR-20260624) | learned / operator |
| W9-013 | Line1/2 похожи на swap | NMR | 0 | **Approve** (review-only) | learned / operator |
| W9-020 | W-9 не подписан | WARNING | 0 | **Approve** | corp / policy |
| W9-030 | это W-8 → другой процесс | NMR | 1 | **Approve** | corp / skill |
| W9-031 | неизвестный налоговый док | NMR | 0 | **Approve** | corp / skill |

\* — hard-правила (001/002/003) и BNK-010 на РАЗМЕЧЕННОМ корпусе не стреляли, но
проверены живыми кейсами прошлых сессий (Rechnung-инвойс, Allianz-email, Jamcorder).

## Скрипт Гейта 1 (панель одобрений) — ~10 минут
1. Открой `https://omen.tail461272.ts.net:8766/ui/rules/approve`.
2. Реши Развилку №1 (BNK-002): если вариант A — нажми **Correct ✎** у BNK-002, поменяй
   `verdict_effect: REJECT` → `NEED_MANUAL_REVIEW` и текст сообщения (черновик пришлю
   в чат), сохрани; затем **Approve**. Если B — просто **Approve**.
3. По таблице выше нажми **Approve** у всех правил с «Approve» (или **Approve all
   pending**, если согласен со всей таблицей разом).
4. Скажи мне решение по Развилке №2 (BNK-027 добавить?) и галочкам №3.
5. Контроль: файл `rules/approvals.json` появился; прогони любой документ — в findings
   больше нет `RULE-GATE`-строки «pending».

После Гейта 1: tier/source из таблицы поедут в YAML метаданными (не влияют на вердикты),
и только `tier: corp` попадёт в корп-версию v1 (SAP_ROLLOUT_PLAN).
