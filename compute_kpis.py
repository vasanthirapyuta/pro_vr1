
#!/usr/bin/env python3
"""
compute_kpis.py

Usage:
    python compute_kpis.py path/to/daily_updates.csv --out kpi_summary.csv --plots_dir kpi_plots

Description:
    - Loads the daily update CSV (template expected columns listed below).
    - Computes KPIs grouped by Sprint (or other column).
    - Writes a KPI summary CSV and saves simple PNG plots for each KPI.
    - Prints a short table to stdout.

Expected columns in the CSV (recommended template):
    Date,Sprint,Name,Attendance,Planned,Type,Title,Azure Link,Status,
    Start Date,Estimated End Date,Final End Date,Delay reason,Hrs,Remarks,
    Automation Applicable,Automation Done,Is Bug,Is CI Task,Test Cases Written,Test Cases Automated
"""

import argparse
import os
import sys
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from textwrap import dedent

def load_sheet(path):
    parse_dates = ['Date','Start Date','Estimated End Date','Final End Date']
    parse_cols = [c for c in parse_dates if c in pd.read_csv(path, nrows=0).columns]
    df = pd.read_csv(path, parse_dates=parse_cols, dayfirst=False, low_memory=False)
    for col in ['Automation Applicable','Automation Done','Is Bug','Is CI Task','Planned']:
        if col in df.columns:
            df[col] = df[col].replace('', pd.NA).fillna('').astype(str).str.strip()
    if 'Status' in df.columns:
        df['Status'] = df['Status'].replace('', pd.NA).fillna('').astype(str).str.strip()
    for col in ['Test Cases Written', 'Test Cases Automated']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    if 'Hrs' in df.columns:
        df['Hrs'] = pd.to_numeric(df['Hrs'], errors='coerce').fillna(0)
    return df

def compute_kpis(df, group_col='Sprint'):
    if group_col in df.columns:
        groups = df.groupby(group_col, sort=True)
    else:
        groups = [('All', df)]
    rows = []
    for name, g in groups:
        total_tasks = len(g)
        completed = g['Status'].str.lower().eq('completed').sum() if 'Status' in g.columns else 0
        if ('Automation Applicable' in g.columns) and ('Automation Done' in g.columns):
            applicable = g['Automation Applicable'].str.lower().eq('yes').sum()
            done = g['Automation Done'].str.lower().eq('yes').sum()
            automation_pct = (done / applicable * 100) if applicable > 0 else (done * 100.0 / total_tasks if total_tasks>0 else 0.0)
        else:
            done = g['Type'].str.lower().str.contains('automation', na=False).sum() if 'Type' in g.columns else 0
            applicable = total_tasks
            automation_pct = (done / applicable * 100) if applicable>0 else 0.0
        if 'Is Bug' in g.columns:
            bugs = g['Is Bug'].str.lower().eq('yes').sum()
        else:
            bugs = g['Type'].str.lower().str.contains('bug', na=False).sum() + g['Title'].str.lower().str.contains('bug', na=False).sum() if ('Type' in g.columns or 'Title' in g.columns) else 0
        if 'Is CI Task' in g.columns:
            total_ci = g['Is CI Task'].str.lower().eq('yes').sum()
            ci_done = g.loc[g['Is CI Task'].str.lower().eq('yes'),'Status'].str.lower().eq('completed').sum()
            ci_stability = (ci_done / total_ci * 100) if total_ci > 0 else None
        else:
            ci_stability = None
        test_hours = pd.to_numeric(g['Hrs'], errors='coerce').fillna(0).sum() if 'Hrs' in g.columns else 0.0
        
        # Calculate postponement metrics
        postpone_mask = pd.Series(False, index=g.index)
        estimation_slip_mask = pd.Series(False, index=g.index)
        
        # 1. Check if there's a delay reason
        if 'Delay reason' in g.columns:
            postpone_mask = postpone_mask | (g['Delay reason'].notna() & (g['Delay reason'].astype(str).str.strip() != '') & (g['Delay reason'].astype(str).str.lower() != 'nan'))
        
        # 2. Check for estimation slip: Final End Date > Estimated End Date
        if ('Estimated End Date' in g.columns) and ('Final End Date' in g.columns):
            estimation_slip_mask = ((~g['Estimated End Date'].isna()) & (~g['Final End Date'].isna()) & (g['Estimated End Date'] < g['Final End Date']))
            postpone_mask = postpone_mask | estimation_slip_mask
        
        task_postpone_rate = (postpone_mask.sum() / total_tasks * 100) if total_tasks>0 else 0.0
        estimation_slip_rate = (estimation_slip_mask.sum() / total_tasks * 100) if total_tasks>0 else 0.0
        if 'Planned' in g.columns:
            unplanned_rate = (g['Planned'].astype(str).str.lower().eq('no').sum() / total_tasks * 100) if total_tasks>0 else 0.0
        else:
            unplanned_rate = None
        tc_written = int(g['Test Cases Written'].fillna(0).sum()) if 'Test Cases Written' in g.columns else 0
        tc_auto = int(g['Test Cases Automated'].fillna(0).sum()) if 'Test Cases Automated' in g.columns else 0
        
        # Calculate automatable test cases (tasks where Automation Applicable = Yes)
        tc_automatable = 0
        if 'Automation Applicable' in g.columns and 'Test Cases Written' in g.columns:
            automatable_mask = g['Automation Applicable'].str.lower().eq('yes')
            tc_automatable = int(g.loc[automatable_mask, 'Test Cases Written'].fillna(0).sum())
        
        # Calculate automation coverage and rate
        automation_coverage = (tc_auto / tc_automatable * 100) if tc_automatable > 0 else 0.0
        test_automation_rate = (tc_auto / tc_written * 100) if tc_written > 0 else 0.0
        
        rows.append({
            group_col: name,
            'Total Tasks': int(total_tasks),
            'Completed Tasks': int(completed),
            'Automation Percentage': round(automation_pct,2),
            'Defects Found': int(bugs),
            'CI Test Stability (%)': round(ci_stability,2) if ci_stability is not None else None,
            'Test Execution Time (hrs)': round(float(test_hours),2),
            'Task Postpone Rate (%)': round(float(task_postpone_rate),2),
            'Estimation Slip Rate (%)': round(float(estimation_slip_rate),2),
            'Unplanned Task Rate (%)': round(float(unplanned_rate),2) if unplanned_rate is not None else None,
            'No of Bugs Identified': int(bugs),
            'Test Cases Written': tc_written,
            'Automatable Test Cases': tc_automatable,
            'Test Cases Automated': tc_auto,
            'Test Automation Coverage (%)': round(automation_coverage, 2),
            'Test Automation Rate (%)': round(test_automation_rate, 2)
        })
    return pd.DataFrame(rows)

def plot_kpis(kpi_df, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    plt_cols = ['Automation Percentage','Defects Found','CI Test Stability (%)','Test Execution Time (hrs)','Task Postpone Rate (%)','Estimation Slip Rate (%)','Unplanned Task Rate (%)','No of Bugs Identified','Test Cases Written','Automatable Test Cases','Test Cases Automated','Test Automation Coverage (%)','Test Automation Rate (%)']
    for col in plt_cols:
        if col in kpi_df.columns:
            try:
                ax = kpi_df.plot(kind='bar', x=kpi_df.columns[0], y=col, legend=False, figsize=(6,4))
                ax.set_ylabel(col)
                ax.set_xlabel(kpi_df.columns[0])
                ax.set_title(col)
                plt.tight_layout()
                fname = os.path.join(out_dir, f"{col.replace(' ','_')}.png")
                plt.savefig(fname)
                plt.close()
            except Exception as e:
                print(f"Failed to plot {col}: {e}", file=sys.stderr)

def print_kpi_table(kpi_df):
    if kpi_df.empty:
        print("No KPI data to show")
        return
    print("\\nKPI Summary:\\n")
    print(kpi_df.to_string(index=False))

def main():
    parser = argparse.ArgumentParser(description="Compute KPIs from daily update CSV")
    parser.add_argument('csv', help='Path to daily update CSV file')
    parser.add_argument('--group', default='Sprint', help='Column to group by (default: Sprint)')
    parser.add_argument('--out', default='kpi_summary.csv', help='Output KPI CSV file path')
    parser.add_argument('--plots_dir', default='kpi_plots', help='Directory to save KPI plots')
    args = parser.parse_args()
    if not os.path.exists(args.csv):
        print(f"CSV file not found: {args.csv}", file=sys.stderr)
        sys.exit(2)
    df = load_sheet(args.csv)
    kpi = compute_kpis(df, group_col=args.group)
    kpi.to_csv(args.out, index=False)
    print(f"KPI summary written to: {args.out}")
    print_kpi_table(kpi)
    plot_kpis(kpi, args.plots_dir)
    print(f"KPI plots saved in: {args.plots_dir}")

if __name__ == '__main__':
    main()
