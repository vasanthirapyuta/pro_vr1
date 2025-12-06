# PA-AMR KPI Toolkit

This repository contains tools to compute KPIs from daily update sheets for PA-AMR warehouse projects,
including a CLI script and a Streamlit dashboard for interactive exploration.

## Structure
- `kpi_tools/` - reusable modules
- `compute_kpis.py` - CLI script to compute KPI CSV and plots
- `streamlit_dashboard.py` - Streamlit app to upload CSV and view KPIs
- `requirements.txt` - Python dependencies

## Usage
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Compute KPIs from CSV:
   ```bash
   python compute_kpis.py path/to/daily_updates.csv --out kpi_summary.csv
   ```
3. Run Streamlit dashboard:
   ```bash
   streamlit run streamlit_dashboard.py
   ```