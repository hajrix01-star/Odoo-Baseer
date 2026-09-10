# بصير — Odoo Baseer

كود التخصيصات المعتمد لنظام بصير على **Odoo 19 Community**.
يشمل 19 موديولًا مخصصًا والاعتماديات الخارجية الموجودة في الإصدار المعتمد.
المستودع مخصص للكود؛ بيانات الموظفين والرواتب والعمليات والمرفقات وكلمات المرور ليست ضمنه.

## الإصدار الحالي

- أصل المصدر: `0e2a0853d858ffc8760d009a34cf17dda68dde45`.
- ملف `release-source.json` يثبت 1183 ملفًا ببصمات SHA256 وصورة Odoo المعتمدة.
- الإضافات المخصصة: `custom_addons`.
- الاعتماديات المستخدمة: `third_party_addons/erp_heritage_19` و`third_party_addons/odoomates_19`.
- التراخيص محفوظة في ملفات كل موديول. الإضافات الخارجية تبقى ملكًا لأصحابها وفق تراخيصها.

## التحقق

```sh
python ops/verify_source.py
```

GitHub Actions يشغّل فحص المصدر عند رفع الكود. هذا فحص سلامة ملفات وصياغة؛ لا يدّعي اختبار تثبيت أودو أو جاهزية السيرفر.

## النقل إلى السيرفر

يلزم نقل قاعدة البيانات الأصلية وملفات filestore بقناة خاصة منفصلة، ثم مطابقتها قبل فتح النظام.
السيرفر والدومين لم يحددا بعد، والنشر الحي التلقائي **غير مفعّل** في هذه النسخة.
راجع [خطة النشر والتحديثات](ops/DEPLOYMENT.md).

The repository contains accepted source only. A fresh install does not reproduce private database configuration or business records. Production migration and deployment require the target-specific setup and checks described in the runbook.
