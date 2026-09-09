"""CSS1 approved master data; VAT publication evidence dated 2026-09-08.

None means unknown, not unregistered. Platform brands are not assertions about
the legal issuer of every invoice. All keys below are permanent seed identities.
"""

# key, Arabic display name, English name/search aliases, tag, VAT, optional leaf
PROVIDERS = (
    ('energy', 'الشركة السعودية للطاقة', 'Saudi Energy | الشركة السعودية للكهرباء | SEC', 'electricity', '300000361310003', 'electricity'),
    ('water', 'شركة المياه الوطنية', 'National Water Company | NWC', 'water', None, 'water'),
    ('stc', 'شركة الاتصالات السعودية', 'Saudi Telecom Company | stc', 'telecom', '300000157210003', 'telecom'),
    ('mobily', 'شركة اتحاد اتصالات', 'Etihad Etisalat Company | Mobily | موبايلي', 'telecom', '300000699600003', 'telecom'),
    ('zain', 'شركة الاتصالات المتنقلة السعودية', 'Mobile Telecommunications Company Saudi Arabia | Zain | زين السعودية', 'telecom', None, 'telecom'),
    ('salam', 'شركة اتحاد سلام للاتصالات', 'Etihad Salam Telecom | Integrated Telecom Company | الاتصالات المتكاملة | سلام', 'telecom', None, 'internet'),
    ('go', 'شركة اتحاد قو للاتصالات', 'Etihad GO Telecom | Etihad Atheeb Telecom | اتحاد عذيب للاتصالات | GO', 'telecom', None, 'telecom'),
    ('municipalities', 'وزارة البلديات والإسكان', 'Ministry of Municipalities and Housing', 'government', None, None),
    ('commerce', 'وزارة التجارة', 'Ministry of Commerce', 'government', None, None),
    ('business_center', 'المركز السعودي للأعمال الاقتصادية', 'Saudi Business Center | المركز السعودي للأعمال', 'government', None, None),
    ('hrsd', 'وزارة الموارد البشرية والتنمية الاجتماعية', 'Ministry of Human Resources and Social Development | HRSD', 'government', None, None),
    ('foreign_affairs', 'وزارة الخارجية', 'Ministry of Foreign Affairs | MOFA', 'government', None, None),
    ('justice', 'وزارة العدل', 'Ministry of Justice | MOJ', 'government', None, None),
    ('passports', 'المديرية العامة للجوازات', 'General Directorate of Passports | Jawazat', 'government', None, None),
    ('gosi', 'المؤسسة العامة للتأمينات الاجتماعية', 'General Organization for Social Insurance | GOSI', 'government', None, None),
    ('zatca', 'هيئة الزكاة والضريبة والجمارك', 'Zakat Tax and Customs Authority | ZATCA', 'government', None, None),
    ('balady', 'منصة بلدي', 'Balady', 'platform', None, None),
    ('mudad', 'منصة مدد', 'Mudad', 'platform', None, None),
    ('muqeem', 'مقيم', 'Muqeem | منصة مقيم', 'platform', None, None),
    ('qiwa', 'منصة قوى', 'Qiwa', 'platform', None, None),
)

# purpose: (native Saudi account code, collision-safe fallback starting code, label)
ACCOUNT_PURPOSES = {
    'permits': (None, '400093', 'Employee residency and work permits | الإقامات وتصاريح العمل'),
    'visa': ('400014', '400094', 'Visa expenses | مصروف التأشيرات'),
    'employee': ('400075', '400095', 'Other employee expenses | مصروف خدمات الموظفين'),
    'ticket': ('400006', '400096', 'Employee leave tickets | تذاكر الموظفين'),
    'insurance': ('400009', '400097', 'Medical insurance | التأمين الطبي'),
    'utilities': ('400018', '400098', 'Water and electricity | المياه والكهرباء'),
    'telecom': ('400020', '400099', 'Telecommunications | الاتصالات والإنترنت'),
    'internet': ('400023', '400101', 'Internet and other communications | الإنترنت والاتصالات الأخرى'),
    'subscriptions': ('400045', '400100', 'Platform subscriptions | اشتراكات المنصات'),
    'licenses': ('400032', '400102', 'License expenses | مصروف الرخص والسجل التجاري'),
    'legal': ('400031', '400103', 'Legal and attestation expenses | التصديقات والخدمات العدلية'),
}

# leaf key, English product name, Arabic product name, account purpose
SERVICES = (
    ('iqama_issue', 'Iqama issuance', 'إصدار إقامة', 'permits'),
    ('iqama_renewal', 'Iqama renewal', 'تجديد إقامة', 'permits'),
    ('work_permit_issue', 'Work permit issuance', 'إصدار رخصة عمل', 'permits'),
    ('work_permit_renewal', 'Work permit renewal', 'تجديد رخصة عمل', 'permits'),
    ('employee_transfer', 'Employee service transfer', 'نقل خدمات الموظف', 'permits'),
    ('profession_change', 'Profession change', 'تعديل المهنة', 'permits'),
    ('visa', 'Visas', 'تأشيرات', 'visa'),
    ('health_certificate_issue', 'Health certificate issuance', 'إصدار شهادة صحية', 'employee'),
    ('health_certificate_renewal', 'Health certificate renewal', 'تجديد شهادة صحية', 'employee'),
    ('medical_exam', 'Medical examination', 'فحص طبي', 'employee'),
    ('ticket', 'Employee flight ticket', 'تذكرة سفر', 'ticket'),
    ('insurance_issue', 'Medical insurance issuance', 'إصدار تأمين طبي', 'insurance'),
    ('insurance_renewal', 'Medical insurance renewal', 'تجديد تأمين طبي', 'insurance'),
    ('processing', 'Employee service processing', 'خدمات تعقيب', 'employee'),
    ('other_employee', 'Other employee service', 'خدمة أخرى', 'employee'),
    ('electricity', 'Electricity', 'الكهرباء', 'utilities'),
    ('water', 'Water and wastewater', 'المياه والصرف الصحي', 'utilities'),
    ('telecom', 'Telecommunications', 'الاتصالات', 'telecom'),
    ('internet', 'Internet', 'الإنترنت', 'internet'),
    ('mudad_subscription', 'Mudad platform subscription', 'اشتراك منصة مدد', 'subscriptions'),
    ('muqeem_subscription', 'Muqeem platform subscription', 'اشتراك مقيم', 'subscriptions'),
    ('qiwa_subscription', 'Qiwa platform subscription', 'اشتراك منصة قوى', 'subscriptions'),
    ('municipal_license', 'Municipal licenses', 'الرخص البلدية', 'licenses'),
    ('commercial_register', 'Commercial registration', 'السجل التجاري', 'licenses'),
    ('attestation', 'Attestations', 'التصديقات', 'legal'),
    ('legal_services', 'Judicial services', 'الخدمات العدلية', 'legal'),
)
