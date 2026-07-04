# Roadmap (operator-driven, 2026-07-03)

Собрано из операторского фидбека (Eureka/Citizens/Denver кейсы + Codex-анализ).
Формула продукта: **evidence-first validator** — модель извлекает, детерминизм и
правила решают, оператор видит ПОЧЕМУ. Формула обучаемости:
`mistake → classify error source → corrected label + scenario tag → rerun same
doc → eval scenario slice → adopt only if no regressions`.

## Сделано (для контекста)
- Пакетный режим: page-level markers (bank_letter/invoice), письмо перевешивает
  инвойс, invoice firewall (чистый инвойс = REJECT).
- ACH vs wire routing раздельно; multi-ABA regex; spaced/boxed EIN + label-anchored
  тип; date-never-TIN; W-9 **зонные vision-пробы** (чекбокс классификации,
  TIN-боксы) — визуальное свидетельство перебивает текстовую догадку.
- Vision-проба подписи (+дата у подписи); typed officer block = NOTE (BNK-026).
- Прецеденты по sha; Save & retrain (label → fewshot → mdmdoc-extract rebuild →
  rerun); эскалация FAST→STRONG (qwen3:14b) по причинам; кастомная модель —
  дефолтный extractor.
- Политики приватности: банковские значения full локально / masked в BTP;
  TIN всегда masked; labels строго masked.
- SAP screenshot compare (лид. нули, XXX-суффикс, bank key в IBAN, оба ABA).
- Training v2 (первая волна): failures-таблица со ссылками open/review,
  per-field метрики с дельтой, diff improved/regressed/unchanged-wrong,
  рекомендации «что делать дальше», learning trace (before→corrected→after)
  на run-странице, фоновые задачи на Dashboard, фильтры прогонов,
  Copy report / Export .md/.json.

## Next (по операторскому приоритету)

### Распознавание / guardrails
1. **Evidence crops в UI**: рядом с каждым finding — мини-кроп зоны (чекбокс,
   TIN-бокс, подпись, счёт/routing). Инфраструктура есть (_render_zone) — нужен
   endpoint + вывод в findings.
2. **Evidence provenance per field**: {value, page, source: model|ocr-regex|
   vision-crop|rule|precedent, confidence} — в extraction.json и в UI.
3. **SWIFT purpose split**: swift_usd / swift_fx / swift_primary (кейс BOFAUS3N
   vs BOFAUS6S) — не перетирать primary FX-кодом.
4. **Verdict text с указанием страницы-источника**: "ACCEPT based on page 3 bank
   letter; invoice page 4 ignored" (mixed_packet=true уже детектится).
5. ~~Supplier payment instructions как явный подтип~~ DONE 2026-07-03
   (payment_instructions + BNK-004 WARNING; remittance-контекст больше не
   считается invoice-признаком — invoice_marks() структурный); чек-лист
   letterhead-признаков — следующий шаг.
6. Email-support exception mode: только WARNING/exception с approver evidence.
7. Quality diagnostics в отчёте: image-only / rotated / low contrast / OCR weak /
   vision crop used.

### Обучаемость (замкнутый цикл) — DONE 2026-07-03 (кроме 13)
8. ~~**Review v2**~~ DONE: error_source (ocr_missed / model_mapped_wrong /
   rule_wrong / doc_type_wrong / workbook_mismatch) + scenario tags
   (scenarios.py — таксономия + автоподсказка из артефактов прогона);
   в веб-форме и CLI review; хранится в label.
9. ~~**Scenario-based eval slices**~~ DONE: eval --scenario <tag> фильтр +
   автоматические срезы по тегам labels в metrics.scenarios /
   last_results.json / секция на Training-странице.
10. ~~**Few-shot по покрытию сценариев**~~ DONE: greedy max-coverage по
    scenario-тегам (+doc_type как вырожденный случай), tie-break по teaching
    value; recency больше не участвует.
11. ~~**Training queue**~~ DONE (training_queue.py): manual-review вердикты,
    эскалации strong-tier, model/evidence конфликты, непокрытые сценарии,
    eval-регрессии (протухший gold) — секция на Training-странице.
12. ~~**Model adoption gate**~~ DONE (adoption.py): Save&retrain теперь строит
    ТОЛЬКО mdmdoc-extract-candidate; гейт-eval (record=False, история
    нетронута) — leakage=0, invoice FA=0, критичные поля (bank.iban/
    account_number/swift_bic, w9.tin/line3) без регресса vs baseline;
    Adopt (ollama cp) / Rollback (Modelfile.mdmdoc-extract.previous) на
    Training-странице; production меняется только через Adopt.
13. Label quality dashboard (missing fields, only-verdict labels, duplicates,
    not-used-in-fewshot).

### W-9 / workbook
14. **W-9 ↔ workbook (.xlsm) reconciliation**: Name 1/2, Tax Number 1/2,
    Recipient Type, address, company code; отдельно "W-9 OK, workbook mismatch".
15. MDM notes: Recipient Type ↔ classification; withholding 07 not default.

### External Evidence Search (модуль web_enrichment — evidence, НЕ verdict)
MVP-порядок: (1) ABA/Fed Routing Directory + FDIC BankFind; (2) SWIFT/BIC
syntax+country (полный lookup — платный коннектор); (3) GLEIF/SEC EDGAR/
OpenCorporates entity match; (4) VIES/SAM.gov/IRS TEOS как страновые коннекторы;
(5) UI-панель "External evidence" с source/timestamp/"web did not decide verdict".
Ограничения: НИКОГДА не отправлять наружу полный TIN/SSN/счёт/IBAN; наружу можно
routing, SWIFT, bank name, company name, VAT. IRS TIN Matching — только через
authorized payer. Source trust tiers: Tier1 IRS/FDIC/Fed/OFAC/SEC/SAM/registry;
Tier2 official domain; Tier3 каталоги; Tier4 не decision-grade.

### Инфраструктура
16. Batch-режим: PDF + workbook + SAP screenshot одним пакетом. Первый шаг DONE
    2026-07-03: единый вход Auto (stage_a.sniff_doc_class), .zip/.eml-контейнеры
    (pipeline._resolve_container: вложения писем распаковываются, лучший документ
    выбирается по page_score + bank-letter бонусу с быстрым OCR стр.1, остальные
    перечислены в warnings); дальше — прогон ВСЕХ документов пакета.
17. Jobs persistence через рестарты сервера (сейчас — in-memory + Dashboard strip).
18. Export отчёта в PDF; RU-версия отчёта целиком.
