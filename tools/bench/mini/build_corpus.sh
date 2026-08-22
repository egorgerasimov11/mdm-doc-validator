#!/bin/bash
# Rebuild bench/corpus.jsonl from the curated folder + donors found on this Mac.
# Re-running is idempotent (rows are keyed by sha256; manual fields win).
set -u
cd "$(dirname "$0")/.."
M="uv run mdmdoc bench manifest add"
T=/Users/egor/Projects/Work/templates
R=/Users/egor/SAP-rescue-20260726/Request-master/Workspace
D=/Users/egor/Downloads

# 1. the curated folder (all of it; per-file refinements below)
$M doct

# canaries / refinements inside doct
$M "doct/Bank account_Pf_Nam.PDF"          --langs ko,en --type "bankbook cover (NH Nonghyup)" --tags core,bankbook
$M "doct/Bank account_Pf_Shon.jpg"         --langs ko,en --type "bankbook cover (photo)" --tags bankbook,photo
$M "doct/Bank account_Pf_Shon copy.jpg"    --langs ko,en --type "bankbook cover (photo)" --tags bankbook,photo
$M "doct/中国银行账户信息-印章版.pdf"         --langs zh --type "bank account information letter with seal" --tags core,seal,bank_letter
$M "doct/2026--开户行许可证.pdf"             --langs zh --type "account opening permit (开户许可证)" --tags seal,permit
$M "doct/W9 - Round Rock 2026_Signed.pdf"  --langs en --type "IRS W-9 (signed, scanned)" --tags core,w9,signature,handwriting
$M "doct/W9 - 2026.pdf"                    --langs en --type "IRS W-9 (scanned)" --tags w9,signature,handwriting
$M "doct/S115 CenturyLink Communications LLC Rev March 2024 (1).pdf" --langs en --type "IRS W-9" --tags w9
$M "doct/Form W-9 (Rev. March 2024) new address.pdf" --langs en --type "IRS W-9 (digital fillable)" --tags w9
$M "doct/W9 (2026)- 50346 IORAD INCORPORATED.pdf" --langs en --type "IRS W-9 (digital fillable)" --tags w9
$M "doct/333468967JULY26.pdf"  --langs en --type "bank statement (scanned)" --tags statement --pages 0,1,7
$M "doct/333468967JUNE26.pdf"  --langs en --type "bank statement (scanned)" --tags statement --pages 0,3
$M "doct/333264473JUNE26.pdf"  --langs en --type "bank statement (scanned)" --tags statement --pages 0,1
$M "doct/syuisyo近畿臨床工学技士会2026 (1).pdf" --langs ja --type "society prospectus (趣意書)" --tags sparse_layer --pages 0,1,2
$M "doct/780327158.pdf"        --langs en --type "remittance advice" --tags remittance --pages 0,1
$M "doct/Email from vendor.pdf" --langs en,nl --type "email thread printout with bank details" --tags email --pages 0,1
$M "doct/Brief companydetails.pdf" --langs nl --type "company details letter (phone capture)" --tags photo
$M "doct/RIB_ATREEC.pdf"       --langs fr --type "RIB (relevé d'identité bancaire)" --tags rib
$M "doct/carta intestata IBAN CP Stampi.pdf" --langs it --type "IBAN letterhead" --tags bank_letter
$M "doct/Dati C.P. Stampi Srl.pdf" --langs it --type "vendor data sheet" --tags vendor_form
$M "doct/Bank Statement.pdf"   --langs nl,en --type "bank statement (digital)" --tags statement
$M "doct/Certificado Bancario Adecco Servicios.pdf" --langs es --type "certificado bancario" --tags bank_letter
$M "doct/Bank Information  2026 IORAD.pdf" --langs en --type "bank information letter" --tags bank_letter
$M "doct/NEW Remittance Micross Components -TX Round Rock (1).pdf" --langs en --type "remittance advice" --tags remittance
$M "doct/07. Project Lifeline - Notice to SORIN Group S.r.l.pdf" --langs en --type "legal notice letter" --tags letter

# 2. donors — CJK
$M "$T/Korea/real_packets/SAP-20260427-002--0538-update-seohyun-accounting-corporation__d54141a5/extracted_email_attachments/서현회계법인_입금통장(kt&g).pdf" --langs ko --type "bankbook (입금통장)" --tags core,bankbook
$M "$T/Korea/real_packets/0538 New Vendor HRNET ONE INC.__fe7b845e/HRKR - HSBC KRW ACCOUNT.pdf" --langs ko,en --type "bank certificate (HSBC KRW)" --tags bank_letter
$M "$R/Completed/requests/2026/05/04/SAP-20260504-002--0538-create-the-korean-society-for-thoracic-cardiovascular-surge/approver-attachments/KSCVTS_Hana_Bank_bankbook.pdf" --langs ko --type "bankbook (Hana Bank)" --tags bankbook
$M "$R/Completed/requests/2026/05/04/SAP-20260504-002--0538-create-the-korean-society-for-thoracic-cardiovascular-surge/approver-attachments/CS_TechPlus_Shinhan_Bank_bankbook.jpg" --langs ko --type "bankbook (Shinhan Bank, photo)" --tags bankbook,photo
$M "$R/Completed/requests/2026/05/12/SAP-20260512-002--0352-create-the-51st-annual-meeting-of-japanese-society-of-extra/request-files/口座情報.pdf" --langs ja --type "bank account information sheet (口座情報)" --tags bank_letter
$M "/Users/egor/Projects/Work/Hot/sap-consolidator/documents/【記入済み銀行口座認証】Lilycolor_20260706.pdf" --langs ja --type "bank account verification form, hand-filled" --tags core,handwriting,form
$M "$T/Japan/real_packets/0352 New Vendor 44TH JASECT TOHOKU__00ae24d1/趣意書_第44回JaSECT東北地方会大会.pdf" --langs ja --type "society prospectus (趣意書)" --tags sparse_layer --pages 0,1
$M "$T/China/real_packets/_ New vendor request-Hunan Medical Doctor Association__7659aec4/Re New vendor request-Hunan Medical Doctor Association/attachments/医师协会开户许可证（盖章）.pdf" --langs zh --type "account opening permit with seal" --tags seal,permit
$M "$T/China/real_packets/New vendor request-Chinese Medical Multimedia Press__8ae5c501/attachments/账户信息.pdf" --langs zh --type "bank account information (phone capture)" --tags photo,bank_letter
$M "$T/China/real_packets/request-files__0708ca15/bank info.pdf" --langs zh --type "bank info (phone capture)" --tags photo,bank_letter
$M "$R/Pending/New vendor creation request-SCIMEA/盖章-四川省国际医学交流促进会银行信息.pdf" --langs zh --type "bank information with seal" --tags seal,bank_letter
$M "$R/Completed/06.04/0538 New Vendor LANDSOFT LTD/request-files/1. Business Registration_LANDSOFT LTD.jpg" --langs en,zh --type "business registration certificate (HK, image)" --tags registration,photo

# 3. donors — Europe / LATAM / MENA
$M "$T/Germany/real_packets/FW New vendor EET Deutschland GmbH__c851da0b/attachments/Bankbestätigung - EET DEUTSCHLAND GMBH.pdf" --langs de --type "Bankbestätigung" --tags bank_letter
$M "$R/Completed/20.04/AW_ new vendor (SWT Services GmbH & Co. KG)/request-files/Kontobestätigung Deutsch.pdf" --langs de --type "Kontobestätigung" --tags bank_letter
$M "$T/Switzerland/real_packets/WG_ Rechnung_ Gutschrift__673bfd3a/Kontodetails zu IBAN CH75 0078 4162 0017 1300 6 von der TKB.pdf" --langs de --type "account details (landscape)" --tags bank_letter,landscape
$M "$T/Switzerland/WG_ Rechnung_ Gutschrift/Rechnung 2026022.pdf" --langs de --type "invoice (Rechnung)" --tags invoice
$M "$T/France/real_packets/SAP-20260504-001--0522-create-guillaume-flipot__9564b661/[EXTERNAL] Frais de repas Mirandola/rib.pdf" --langs fr --type "RIB" --tags rib
$M "$T/France/real_packets/SAP-20260427-004--0298-create-pavupapri-srl__fb60f196/Déclaration de compte nominative.pdf" --langs fr --type "déclaration de compte" --tags bank_letter
$M "$T/Italy/real_packets/IBAN CHANGE - Valentina Profiti__1feecc91/attachments/Titolarita_Conto.pdf" --langs it --type "account ownership statement" --tags bank_letter
$M "$T/Italy/real_packets/0281 New Vendor ILENIA SOCCIO__c84b4fd4/20260416_120803.pdf" --langs it --type "bank details (scan/photo)" --tags photo,bank_letter
$M "$T/Poland/real_packets/VENDOR 54530 - UPDATE euro-net__172cd3a1/attachments/oświdczenie 30.06.2026.pdf" --langs pl --type "account declaration (oświadczenie)" --tags bank_letter
$M "$D/Fw_ vendor 19590 Orbis - update/certyfitak bankowy PL.pdf" --langs pl --type "bank certificate" --tags bank_letter
$M "$D/Fw_ [EXTERNAL] Re_ 49411; DAVID SZLOVAK; TEST WIRE/Szolgáltatás igénylő vagy módosító.PDF" --langs hu --type "bank service request form" --tags form --pages 0,1
$M "$T/Brazil/real_packets/VENDER REGISTRATION - Bertami Solucoes e Projetos LTDA (1)__9403684d/Ficha Cadastral - BERTAMI SOLUÇÕES 1.pdf" --langs pt --type "vendor registration form (ficha cadastral)" --tags vendor_form
$M "$T/Chile/real_packets/_NEW VENDOR CREATION_ Logi_stica Me_dica SpA__384a4be9/attachments/Certificado Banco Scotiabank.pdf" --langs es --type "certificado bancario" --tags bank_letter
$M "$T/Chile/real_packets/RV 45373 0527 Complete FW _VENDOR CREATION_ CORDADA SPA__ae96f89e/attachments/Certificado Bancario Cordada SpA-Jun-17-2026-02-26-05-9552-PM.pdf" --langs es --type "certificado bancario (scanned)" --tags bank_letter
$M "$T/Colombia/real_packets/RV New vendor - Colombia HEALTH COMPANY INT. S.A.S__01ec4377/attachments/10.CERTIFICACION BANCARIA DAVIVIENDA CORRIENTE.pdf" --langs es --type "certificación bancaria" --tags bank_letter
$M "$D/New Vendor_ ATLAS TRAVEL & TOURISM AGENCY (1)/‎⁨شهادة بالرقم الضريبي 31-3-2026⁩.pdf" --langs ar,en --type "tax registration certificate" --tags certificate
$M "$D/New Vendor_ ATLAS TRAVEL & TOURISM AGENCY (1)/BANK LETTER2.pdf" --langs ar,en --type "bank letter" --tags bank_letter
$M "$D/Telegram Desktop/Квитанция.pdf" --langs ru --type "payment receipt (квитанция)" --tags receipt

# 4. donors — US tax forms, checks, photos, long packets
$M "$D/13-Denver 2026 W9.pdf" --langs en --type "IRS W-9 (scanned)" --tags w9,signature
$M "$T/USA/real_packets/41956 Sigma-Aldrich_ Inc Update Banking__cfa1a017/attachments/2106 - Sigma-Aldrich Inc-W9-2026.pdf" --langs en --type "IRS W-9 (scanned)" --tags w9
$M "$T/USA/Fw_ MDM for New Vendor - John Zajecka as Consultant - Updated W-9 & Rev MDM form/Zajecka W-9 completed 6.08.26.pdf" --langs en --type "IRS W-9 (hand-completed)" --tags w9,handwriting
$M "$D/FW_ 47442 THE HOSPITAL FOR SICK CHILDREN (1)/Form W-8BEN-E_Jan2026.pdf" --langs en --type "IRS W-8BEN-E (digital)" --tags w8 --pages 0,1,7
$M "$D/Telegram Desktop/Graphic Center W-8BEN-E.pdf" --langs en --type "IRS W-8BEN-E (scanned)" --tags core,w8 --pages 0,1,7
$M "$D/Telegram Desktop/W-8BEN Meyer.pdf" --langs en --type "IRS W-8BEN (individual)" --tags w8
$M "$D/RE_ New Vendor request fpr Internal Audit - Eric Friedberg (Next Tier Cybersecurity)/Voided signed check.pdf" --langs en --type "voided check (photo)" --tags check,photo,handwriting
$M "$D/EC-Karte.jpeg" --langs de --type "debit card photo" --tags core,photo
$M "$D/Fw_ Vendor BW/Landesv. der Epilepsie BW e.V._6532.pdf" --langs de --type "letter (phone capture)" --tags photo
$M "$D/CARATULA MAYO ESTADO DE CUENTA MAKE MARKETING.png" --langs es --type "bank statement cover page (image)" --tags statement,image_file
$M "$R/Completed/06.04/0601 0432 Vendor Extend SHERLOCK PR & COMMUNICATIONS LTD/request-files/Foreign Currency Statement 30-01-26_Redacted.pdf" --langs en --type "bank statement (scanned, redacted)" --tags statement --pages 0,1
$M "$D/Telegram Desktop/packet (3).pdf" --langs es,en --type "vendor packet (42 pages, sparse layer)" --tags packet --pages 0,1,5,20

# 5. synthetic stratum (exact gold = text layer)
uv run mdmdoc bench manifest build-synthetic
uv run mdmdoc bench manifest show all
