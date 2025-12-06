# KPI Calculations Reference Guide

This document explains how each KPI is calculated, the formulas used, and where to find the calculation logic in the code.

---

## 📊 KPI Metrics Overview

All KPI calculations are performed in the `compute_kpis()` function located in:
- **Primary:** `compute_kpis.py` (lines 45-120)
- **Backup:** `kpi_tools/compute_kpis.py` (lines 45-120)

---

## 1. Total Tasks
**Formula:** Count of all rows/tasks in the sprint/group

**Calculation:**
```python
total_tasks = len(g)
```

**Code Location:** `compute_kpis.py` line 52

---

## 2. Completed Tasks
**Formula:** Count of tasks where Status = "Completed"

**Calculation:**
```python
completed = g['Status'].str.lower().eq('completed').sum()
```

**Code Location:** `compute_kpis.py` line 53

**Data Required:**
- Column: `Status`
- Expected values: "Completed", "In Progress", "Review", etc. (case-insensitive)

---

## 3. Automation Percentage
**Formula:** 
- *If "Automation Applicable" and "Automation Done" columns exist:*
  ```
  (Tasks with Automation Done=Yes / Tasks with Automation Applicable=Yes) × 100
  ```
- *Fallback:*
  ```
  (Tasks where Type contains "automation" / Total Tasks) × 100
  ```

**Calculation:**
```python
if ('Automation Applicable' in g.columns) and ('Automation Done' in g.columns):
    applicable = g['Automation Applicable'].str.lower().eq('yes').sum()
    done = g['Automation Done'].str.lower().eq('yes').sum()
    automation_pct = (done / applicable * 100) if applicable > 0 else (done * 100.0 / total_tasks)
else:
    done = g['Type'].str.lower().str.contains('automation', na=False).sum()
    applicable = total_tasks
    automation_pct = (done / applicable * 100)
```

**Code Location:** `compute_kpis.py` lines 54-61

**Data Required:**
- Primary: `Automation Applicable` (Yes/No), `Automation Done` (Yes/No)
- Fallback: `Type` (text containing "automation")

---

## 4. Defects Found
**Formula:** Count of tasks marked as bugs

**Calculation:**
```python
if 'Is Bug' in g.columns:
    bugs = g['Is Bug'].str.lower().eq('yes').sum()
else:
    bugs = g['Type'].str.lower().str.contains('bug', na=False).sum() + 
           g['Title'].str.lower().str.contains('bug', na=False).sum()
```

**Code Location:** `compute_kpis.py` lines 62-65

**Data Required:**
- Primary: `Is Bug` (Yes/No)
- Fallback: `Type` or `Title` containing "bug" (case-insensitive)

**Note:** "Defects Found" and "No of Bugs Identified" are the same value.

---

## 5. CI Test Stability (%)
**Formula:** 
```
(Completed CI Tasks / Total CI Tasks) × 100
```

**Calculation:**
```python
if 'Is CI Task' in g.columns:
    total_ci = g['Is CI Task'].str.lower().eq('yes').sum()
    ci_done = g.loc[g['Is CI Task'].str.lower().eq('yes'),'Status'].str.lower().eq('completed').sum()
    ci_stability = (ci_done / total_ci * 100) if total_ci > 0 else None
```

**Code Location:** `compute_kpis.py` lines 66-70

**Data Required:**
- Column: `Is CI Task` (Yes/No)
- Column: `Status` (Completed/In Progress/etc.)

---

## 6. Test Execution Time (hrs)
**Formula:** Sum of all hours spent on tasks

**Calculation:**
```python
test_hours = pd.to_numeric(g['Hrs'], errors='coerce').fillna(0).sum()
```

**Code Location:** `compute_kpis.py` line 71

**Data Required:**
- Column: `Hrs` (numeric value)

---

## 7. Task Postpone Rate (%)
**Formula:** 
```
(Tasks with Delay Reason OR Final End Date > Estimated End Date) / Total Tasks × 100
```

**Calculation:**
```python
postpone_mask = pd.Series(False, index=g.index)

# Check for delay reason
if 'Delay reason' in g.columns:
    postpone_mask = postpone_mask | (g['Delay reason'].notna() & 
                                     (g['Delay reason'].astype(str).str.strip() != '') & 
                                     (g['Delay reason'].astype(str).str.lower() != 'nan'))

# Check for estimation slip
if ('Estimated End Date' in g.columns) and ('Final End Date' in g.columns):
    estimation_slip_mask = ((~g['Estimated End Date'].isna()) & 
                           (~g['Final End Date'].isna()) & 
                           (g['Estimated End Date'] < g['Final End Date']))
    postpone_mask = postpone_mask | estimation_slip_mask

task_postpone_rate = (postpone_mask.sum() / total_tasks * 100)
```

**Code Location:** `compute_kpis.py` lines 73-87

**Data Required:**
- Column: `Delay reason` (text) - Optional
- Column: `Estimated End Date` (date)
- Column: `Final End Date` (date)

---

## 8. Estimation Slip Rate (%)
**Formula:** 
```
(Tasks where Final End Date > Estimated End Date) / Total Tasks × 100
```

**Calculation:**
```python
estimation_slip_mask = ((~g['Estimated End Date'].isna()) & 
                       (~g['Final End Date'].isna()) & 
                       (g['Estimated End Date'] < g['Final End Date']))
estimation_slip_rate = (estimation_slip_mask.sum() / total_tasks * 100)
```

**Code Location:** `compute_kpis.py` lines 82-88

**Data Required:**
- Column: `Estimated End Date` (date)
- Column: `Final End Date` (date)

---

## 9. Unplanned Task Rate (%)
**Formula:** 
```
(Tasks where Planned = "No") / Total Tasks × 100
```

**Calculation:**
```python
if 'Planned' in g.columns:
    unplanned_rate = (g['Planned'].astype(str).str.lower().eq('no').sum() / total_tasks * 100)
else:
    unplanned_rate = None
```

**Code Location:** `compute_kpis.py` lines 89-92

**Data Required:**
- Column: `Planned` (Yes/No)

---

## 10. Test Cases Written
**Formula:** Sum of all test cases created

**Calculation:**
```python
tc_written = int(g['Test Cases Written'].fillna(0).sum())
```

**Code Location:** `compute_kpis.py` line 93

**Data Required:**
- Column: `Test Cases Written` (numeric value)

---

## 11. Automatable Test Cases
**Formula:** Sum of "Test Cases Written" where "Automation Applicable" = Yes

**Calculation:**
```python
tc_automatable = 0
if 'Automation Applicable' in g.columns and 'Test Cases Written' in g.columns:
    automatable_mask = g['Automation Applicable'].str.lower().eq('yes')
    tc_automatable = int(g.loc[automatable_mask, 'Test Cases Written'].fillna(0).sum())
```

**Code Location:** `compute_kpis.py` lines 96-99

**Data Required:**
- Column: `Automation Applicable` (Yes/No)
- Column: `Test Cases Written` (numeric value)

---

## 12. Test Cases Automated
**Formula:** Sum of all test cases automated

**Calculation:**
```python
tc_auto = int(g['Test Cases Automated'].fillna(0).sum())
```

**Code Location:** `compute_kpis.py` line 94

**Data Required:**
- Column: `Test Cases Automated` (numeric value)

---

## 13. Test Automation Coverage (%)
**Formula:** 
```
(Test Cases Automated / Automatable Test Cases) × 100
```

**Calculation:**
```python
automation_coverage = (tc_auto / tc_automatable * 100) if tc_automatable > 0 else 0.0
```

**Code Location:** `compute_kpis.py` line 101

**Data Required:**
- Columns: `Test Cases Automated`, `Automatable Test Cases` (calculated)

**Interpretation:** Shows what percentage of automatable tests have been automated

---

## 14. Test Automation Rate (%)
**Formula:** 
```
(Test Cases Automated / Test Cases Written) × 100
```

**Calculation:**
```python
test_automation_rate = (tc_auto / tc_written * 100) if tc_written > 0 else 0.0
```

**Code Location:** `compute_kpis.py` line 102

**Data Required:**
- Columns: `Test Cases Automated`, `Test Cases Written`

**Interpretation:** Shows overall automation rate across all test cases

---

## 🔧 Troubleshooting KPI Mismatches

### If a KPI shows unexpected values:

1. **Check Data Completeness:**
   - Verify required columns exist in CSV
   - Check for empty/null values in key columns
   - Ensure date columns are properly formatted

2. **Verify Column Names:**
   - Column names are case-sensitive in some contexts
   - Check for extra spaces in column headers
   - Ensure column names match exactly as shown in this document

3. **Check Data Types:**
   - Dates should be in format: DD-MMM-YYYY (e.g., "3-Nov-2025")
   - Yes/No fields are case-insensitive but should be "Yes" or "No"
   - Numeric fields (Hrs, Test Cases) should contain numbers only

4. **Review Load Function:**
   - Check `load_sheet()` function in `compute_kpis.py` (lines 27-42)
   - This function preprocesses data before KPI calculation
   - Ensures proper data type conversions

5. **Enable Debug Output:**
   - Add print statements in `compute_kpis()` to see intermediate values
   - Example:
     ```python
     print(f"Sprint: {name}")
     print(f"Total tasks: {total_tasks}")
     print(f"Bugs found: {bugs}")
     ```

---

## 📋 Required CSV Columns Summary

### Mandatory Columns:
- `Date` - Task date
- `Sprint` - Sprint identifier
- `Name` - Person name
- `Status` - Task status

### Important for KPIs:
- `Planned` (Yes/No) - For Unplanned Task Rate
- `Type` - Task type (Testing, Automation, Bug, etc.)
- `Automation Applicable` (Yes/No) - For automation metrics
- `Automation Done` (Yes/No) - For automation metrics
- `Is Bug` (Yes/No) - For defect tracking
- `Is CI Task` (Yes/No) - For CI Test Stability
- `Test Cases Written` (number) - For test case metrics
- `Test Cases Automated` (number) - For automation coverage
- `Estimated End Date` (date) - For postponement metrics
- `Final End Date` (date) - For postponement metrics
- `Delay reason` (text) - For postponement tracking
- `Hrs` (number) - For time tracking

---

## 📖 Additional Notes

### Date Handling:
- Dates are parsed using pandas `to_datetime()` with mixed format support
- Expected format: "3-Nov-2025" or "11/11/2025"
- Empty dates are treated as NaN (Not a Number)

### String Comparisons:
- All string comparisons are case-insensitive (converted to lowercase)
- Empty strings and NaN values are handled separately

### Grouping:
- KPIs are calculated per Sprint by default
- Can be grouped by any column (e.g., Name for per-person KPIs)

### Dashboard Display:
- Located in `streamlit_dashboard.py` (lines 40-60)
- Chart columns defined in line 46-47
- Modify these to add/remove KPIs from charts

---

## 🔗 File References

| File | Purpose | Key Functions |
|------|---------|---------------|
| `compute_kpis.py` | Main KPI calculation | `load_sheet()`, `compute_kpis()`, `plot_kpis()` |
| `kpi_tools/compute_kpis.py` | Backup/CLI version | Same as above |
| `streamlit_dashboard.py` | Web dashboard UI | Data loading, visualization |
| `sentinels-data-sample-1.csv` | Sample data file | Template structure |

---

**Last Updated:** December 4, 2025  
**Version:** 1.0
