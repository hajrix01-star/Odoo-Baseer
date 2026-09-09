# Category labels

Product category labels default to the category's own name. This shared model behavior applies to ordinary category selectors in modules using `product.category`; other category models are outside this addon.

Odoo's `hierarchical_naming=True` context still requests the full name. The stored `complete_name`, parent relationships, full-path searches and category IDs remain unchanged. A separate full parent column in the native category list allows users to distinguish duplicate leaf names through Search More.

No new accounting rules, stored business fields, JavaScript or core edits. Installation is currently limited to QA.
