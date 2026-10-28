# TDD log — piece F1 (Frappe payroll read path + PF)

Branch feat/piece-f1-frappe-payroll (stacked on E @ 7819e9e). Frappe doctype fields verified 2026-10-29 from frappe/hrms develop (Salary Slip, Salary Detail, Salary Structure Assignment) and REST conventions from docs.frappe.io.

## F1-a — rules, fingerprint, validation, computation (pure core)
- RED: `ModuleNotFoundError: No module named 'hrms_plugin.payroll'`.
- GREEN: 41 passed on first run → mutation pass on pf.py (6 mutants): 4 killed; 2 survivors were real gaps:
  - ROUND_HALF_UP vs banker's rounding indistinguishable (1249.5 rounds to 1250 both ways) → added wage 8337.50 → 1000.50 → 1001.
  - employer ceiling flag swapped with employee flag → added mixed case (employee capped, employer on full wage: 1800 / 3000 / EPS 1250 / EPF 1750).
  Both re-run → killed. 43 passed; full suite 831.
- HR arithmetic checked by hand: 12,000 → 1440/1440/EPS 1000/EPF 440; 15,000 → 1800/1800/EPS 1250 (statutory max)/550; 25,000 capped → same as 15,000.
