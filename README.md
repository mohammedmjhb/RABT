# RABT Ledger Engine (محرك الدفتر)

## التشغيل
    pip install -r requirements.txt
    export LEDGER_API_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
    export LEDGER_DB_PATH=/var/lib/rabt/ledger.db     # اختياري
    uvicorn main:app --host 127.0.0.1 --port 8100 --workers 1

## الاختبارات
    python3 -m unittest -v test_ledger_core

## المسارات (كلها تتطلب Authorization: Bearer <LEDGER_API_KEY> و Idempotency-Key للكتابة)
| Method | Path | الوظيفة |
|---|---|---|
| POST | /v1/deposits | إيداع في محفظة |
| POST | /v1/escrow/hold | حجز مبلغ الخدمة (HELD) |
| POST | /v1/escrow/{id}/release | إفراج: 90% للمهندس و10% عمولة |
| POST | /v1/escrow/{id}/refund | استرداد للعميل (من HELD فقط) |
| POST | /v1/escrow/{id}/freeze | تجميد بسبب نزاع |
| POST | /v1/escrow/{id}/resolve | حسم النزاع: {"outcome":"release"\|"refund"} (من FROZEN فقط) |
| GET | /v1/escrow/{id} · /v1/accounts/{user}/balance · /v1/ledger/verify | قراءة وتحقق |

## قواعد مهمة
- المبالغ نصوص عشرية ("12.500") وتخزَّن أعداداً صحيحة بالدرهم (1 دينار = 1000). لا floats.
- الـ Idempotency-Key يجب أن يكون ثابتاً لكل عملية منطقية في Flask (مثلاً `hold-{service_id}`) فيُعاد الطلب بأمان عند انقطاع الشبكة.
- المحرك لا يقرر من يملك صلاحية الإفراج أو الاسترداد: Flask يتحقق من الدور ثم يستدعي المحرك. المحرك يمنع فقط القفز فوق حالة النزاع، والإفراج المزدوج، والرصيد السالب.
- شغّله على `127.0.0.1` أو شبكة خاصة فقط، خلف mTLS أو جدار شبكي. لا تعرّضه للإنترنت.
- `--workers 1` مع SQLite. عند الانتقال إلى PostgreSQL يمكن زيادة العمال.

## ربط Flask (مثال)
    import os, requests
    LEDGER = "http://127.0.0.1:8100"
    H = {"Authorization": f"Bearer {os.environ['LEDGER_API_KEY']}"}
    def ledger_hold(svc, cust, eng, amount):
        r = requests.post(f"{LEDGER}/v1/escrow/hold", headers={**H, "Idempotency-Key": f"hold-{svc}"},
                          json={"service_id": svc, "customer_id": cust, "engineer_id": eng, "amount": amount}, timeout=10)
        r.raise_for_status(); return r.json()
