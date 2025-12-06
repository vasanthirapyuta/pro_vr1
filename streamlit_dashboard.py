
import streamlit as st
import pandas as pd
import plotly.express as px
import os
import tempfile
import re
import base64
import requests
from datetime import datetime
from compute_kpis import load_sheet, compute_kpis

st.set_page_config(page_title="PA-AMR KPI Dashboard", layout="wide")

st.title("PA-AMR KPI Dashboard")
st.markdown("Upload your daily update CSV to compute KPI summaries and visualize KPIs per sprint.")

# Create uploads folder if it doesn't exist
uploads_folder = '/home/vasanthi/Downloads/QA-kpi/kpi_project/uploaded_data'
os.makedirs(uploads_folder, exist_ok=True)

# Default data file path
default_file = '/home/vasanthi/Downloads/QA-kpi/kpi_project/sentinels-data-sample-1.csv'

# Sidebar for file management
with st.sidebar:
    st.header("📁 File Management")
    st.markdown(f"**Upload Folder:** `{uploads_folder}`")
    
    # Show recently uploaded files
    if os.path.exists(uploads_folder):
        uploaded_files = [f for f in os.listdir(uploads_folder) if f.endswith('.csv')]
        if uploaded_files:
            st.subheader("Recently Uploaded Files:")
            for file in sorted(uploaded_files, reverse=True)[:5]:
                st.text(f"• {file}")
    
    st.markdown("""
    **How to Use:**
    1. Upload a CSV file below
    2. File will be saved in the uploads folder
    3. Data will load automatically
    4. KPIs will be calculated and displayed
    """)

uploaded = st.file_uploader("📤 Upload KPI Data CSV", type=['csv'])

# Load data from uploaded file or default file
df = None
current_file_name = None

def add_sprint_column(data):
    """Add Sprint column based on date if not present"""
    if 'Sprint' not in data.columns or data['Sprint'].isna().all():
        # Define sprint date ranges
        sprint_ranges = [
            ('PI4-sprint3', pd.Timestamp('2025-10-27'), pd.Timestamp('2025-11-07')),
            ('PI4-sprint4', pd.Timestamp('2025-11-10'), pd.Timestamp('2025-11-21')),
            ('PI4-sprint5', pd.Timestamp('2025-11-24'), pd.Timestamp('2025-12-31'))
        ]
        
        # Parse dates
        data['ParsedDate'] = pd.to_datetime(data['Date'], format='mixed', dayfirst=True, errors='coerce')
        
        # Assign sprint based on date
        def assign_sprint(date):
            if pd.notna(date):
                for sprint_name, start_date, end_date in sprint_ranges:
                    if start_date <= date <= end_date:
                        return sprint_name
            return ''
        
        data['Sprint'] = data['ParsedDate'].apply(assign_sprint)
        data = data.drop('ParsedDate', axis=1)
        
        st.info("✅ Sprint column auto-populated based on dates (PI4-sprint3, PI4-sprint4, PI4-sprint5)")
    
    return data

if uploaded is not None:
    try:
        # Save uploaded file to uploads folder with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_file_name = f"sentinels_data_{timestamp}.csv"
        saved_file_path = os.path.join(uploads_folder, saved_file_name)
        
        with open(saved_file_path, 'wb') as f:
            f.write(uploaded.getvalue())
        
        # Load the saved file
        df = load_sheet(saved_file_path)
        
        # Add Sprint column if missing
        df = add_sprint_column(df)
        
        # Save updated file with Sprint column
        df.to_csv(saved_file_path, index=False)
        
        current_file_name = saved_file_name
        
        st.success(f"✅ File uploaded and saved successfully!")
        st.info(f"📁 Saved as: `{saved_file_name}`")
        
    except Exception as e:
        st.error(f"❌ Failed to read CSV: {e}")
        st.stop()
elif os.path.exists(default_file):
    try:
        df = load_sheet(default_file)
        current_file_name = os.path.basename(default_file)
        st.info(f"📁 Using default file: `{current_file_name}`")
    except Exception as e:
        st.error(f"Failed to read default CSV: {e}")
        st.stop()

if df is not None:
    st.markdown("---")
    
    # Data preview
    with st.expander("📊 Raw Data Preview (First 50 rows)", expanded=False):
        st.dataframe(df.head(50), use_container_width=True)

    # KPI Calculation
    group_col = 'Sprint' if 'Sprint' in df.columns else 'Name'
    kpi_df = compute_kpis(df, group_col)

    # KPI Summary Table
    st.subheader("📈 KPI Summary")
    st.dataframe(kpi_df, use_container_width=True)

    # Download KPI Summary
    csv_data = kpi_df.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="⬇️ Download KPI Summary as CSV",
        data=csv_data,
        file_name=f"kpi_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv"
    )

    # Combined KPI Dashboard per Sprint
    st.subheader("🎯 Combined KPI Dashboard by Sprint")
    
    # Select key metrics to display together
    key_metrics = {
        'Defects Found': 'Defects Found',
        'Task Postpone Rate (%)': 'Task Postpone Rate (%)',
        'Test Cases Written': 'Test Cases Written',
        'Test Cases Automated': 'Test Cases Automated',
        'Test Automation Coverage (%)': 'Test Automation Coverage (%)',
        'Automation Percentage': 'Automation Percentage'
    }
    
    # Filter to only available metrics
    available_metrics = [m for m in key_metrics.values() if m in kpi_df.columns]
    
    if available_metrics:
        # Create combined metrics table
        combined_df = kpi_df[[group_col] + available_metrics].copy()
        
        # Create combined chart with subplots
        import plotly.subplots as sp
        
        fig = sp.make_subplots(
            rows=2, cols=3,
            subplot_titles=available_metrics[:6],
            specs=[[{'secondary_y': False}, {'secondary_y': False}, {'secondary_y': False}],
                   [{'secondary_y': False}, {'secondary_y': False}, {'secondary_y': False}]]
        )
        
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b']
        
        for idx, metric in enumerate(available_metrics[:6]):
            row = idx // 3 + 1
            col = idx % 3 + 1
            
            fig.add_trace(
                px.bar(combined_df, x=group_col, y=metric, color=group_col).data[0],
                row=row, col=col
            )
            
            fig.update_yaxes(title_text=metric, row=row, col=col)
        
        fig.update_layout(height=700, showlegend=False, title_text="📊 Combined KPI Metrics per Sprint")
        st.plotly_chart(fig, use_container_width=True)
        
        # Display combined metrics table
        st.markdown("**Combined Metrics Summary:**")
        st.dataframe(combined_df, use_container_width=True)
    
    st.markdown("---")
    
    # Individual KPI Charts
    st.subheader("📊 Individual KPI Charts")
    cols = ['Automation Percentage','Defects Found','CI Test Stability (%)','Test Execution Time (hrs)','Task Postpone Rate (%)','Estimation Slip Rate (%)','Unplanned Task Rate (%)','No of Bugs Identified','Test Cases Written','Automatable Test Cases','Test Cases Automated','Test Automation Coverage (%)','Test Automation Rate (%)']
    chart_cols = [c for c in cols if c in kpi_df.columns]
    
    for c in chart_cols:
        try:
            fig = px.bar(kpi_df, x=group_col, y=c, title=f"📊 {c}", color=group_col)
            st.plotly_chart(fig, use_container_width=True)
        except:
            st.warning(f"Could not plot {c}")

    # Per-person KPIs
    st.subheader("👤 Per-Person KPIs")
    persons = df['Name'].dropna().unique().tolist() if 'Name' in df.columns else []
    selected = st.selectbox("Select team member:", options=["All"] + persons)
    
    if selected != "All":
        person_df = df[df['Name']==selected]
        p_kpi = compute_kpis(person_df, group_col='Sprint' if 'Sprint' in person_df.columns else 'All')
        
        st.subheader(f"KPIs for {selected}")
        st.dataframe(p_kpi, use_container_width=True)
        
        for c in chart_cols:
            try:
                fig = px.bar(p_kpi, x=p_kpi.columns[0], y=c, title=f"{c} - {selected}")
                st.plotly_chart(fig, use_container_width=True)
            except:
                pass

    # Footer with file info
    st.markdown("---")
    st.markdown(f"**Current Data File:** `{current_file_name}`")
    st.markdown(f"**Total Tasks:** {len(df)}")
    if 'Sprint' in df.columns:
        st.markdown(f"**Sprints:** {', '.join(df['Sprint'].dropna().unique())}")
    # -------------------------
    # Azure DevOps cross-check
    # -------------------------
    st.markdown("---")
    st.subheader("🔗 Azure DevOps Cross-check")
    st.markdown("Provide Azure DevOps details and click 'Run check' to validate Azure work items referenced in the sheet.")

    with st.expander("Azure DevOps Settings", expanded=False):
        st.markdown("**Note:** Enter your PAT below — it will only be used in memory and not stored to disk.")
        az_org = st.text_input("Organization (e.g. myorg)", value="rapyuta-robotics")
        az_project = st.text_input("Project (e.g. myproject)", value="sootballs")
        # use a session_state-backed text_input for PAT so we can clear it after use
        az_pat = st.text_input("Personal Access Token (PAT)", type="password", key="az_pat")
        run_check = st.button("Run Azure cross-check")

    def fetch_work_items_by_ids(organization, project, ids, pat_token):
        """Fetch work item details for given IDs from Azure DevOps."""
        if not ids:
            return []
        try:
            auth_string = base64.b64encode(f":{pat_token}".encode()).decode()
            headers = {
                'Authorization': f'Basic {auth_string}',
                'Content-Type': 'application/json'
            }
            ids_string = ','.join(map(str, ids))
            details_url = f"https://dev.azure.com/{organization}/{project}/_apis/wit/workitems?ids={ids_string}&api-version=6.0"
            resp = requests.get(details_url, headers=headers)
            if resp.status_code != 200:
                st.error(f"Azure API error: {resp.status_code} - {resp.text}")
                return []
            return resp.json().get('value', [])
        except Exception as e:
            st.error(f"Error fetching work items: {e}")
            return []

    # When user clicks run_check, perform validations
    if 'Azure Link' in df.columns and run_check:
        if not az_org or not az_project or not az_pat:
            st.warning("Please provide Organization, Project and PAT to run the check.")
        else:
            st.info("Looking up Azure work items referenced in the sheet...")

            # Extract IDs from Azure Link column
            def extract_wi_id(url):
                try:
                    if pd.isna(url):
                        return None
                    # Common pattern: /_workitems/edit/12345/ or .../workitems/12345
                    m = re.search(r"/edit/(\d+)", str(url)) or re.search(r"workitems/(\d+)", str(url)) or re.search(r"(?:id=|/)(\d+)(?:$|/)", str(url))
                    return int(m.group(1)) if m else None
                except:
                    return None

            df['__WI_ID__'] = df['Azure Link'].apply(extract_wi_id)
            ids = sorted({int(i) for i in df['__WI_ID__'].dropna().unique()})
            st.write(f"Found {len(ids)} unique work item IDs referenced in sheet")

            # fetch in batches of 200
            fetched = []
            batch_size = 200
            for i in range(0, len(ids), batch_size):
                batch = ids[i:i+batch_size]
                fetched_batch = fetch_work_items_by_ids(az_org, az_project, batch, az_pat)
                fetched.extend(fetched_batch)

            # Build mapping id -> fields
            wi_map = {int(wi['id']): wi for wi in fetched}

            # Compare per-row
            results = []
            for idx, row in df.iterrows():
                wi_id = row.get('__WI_ID__')
                link = row.get('Azure Link')
                title_local = str(row.get('Title', '')).strip()
                status_local = str(row.get('Status', '')).strip().lower()

                found = False
                wi_state = None
                wi_title = None
                if pd.notna(wi_id) and int(wi_id) in wi_map:
                    found = True
                    wi = wi_map[int(wi_id)]
                    fields = wi.get('fields', {})
                    wi_title = fields.get('System.Title')
                    wi_state = fields.get('System.State')

                title_match = False
                status_match = False
                if found and wi_title and title_local:
                    title_match = title_local.lower() in str(wi_title).lower()
                if found and wi_state and status_local:
                    status_match = status_local == str(wi_state).strip().lower()

                results.append({
                    'row_index': int(idx),
                    'Azure Link': link,
                    'WorkItemID': int(wi_id) if pd.notna(wi_id) else None,
                    'FoundInAzure': found,
                    'AzureTitle': wi_title,
                    'AzureState': wi_state,
                    'LocalTitle': title_local,
                    'LocalStatus': status_local,
                    'TitleMatch': title_match,
                    'StatusMatch': status_match
                })

            res_df = pd.DataFrame(results)
            st.subheader("Azure Cross-check Results")
            st.dataframe(res_df.head(200), use_container_width=True)

            # Summary
            total_refs = len(res_df)
            found_cnt = int(res_df['FoundInAzure'].sum())
            title_match_cnt = int(res_df['TitleMatch'].sum())
            status_match_cnt = int(res_df['StatusMatch'].sum())
            st.markdown(f"**Total referenced rows:** {total_refs}")
            st.markdown(f"**Found in Azure:** {found_cnt}")
            st.markdown(f"**Title matches:** {title_match_cnt}")
            st.markdown(f"**Status matches:** {status_match_cnt}")

            csv_out = res_df.to_csv(index=False).encode('utf-8')
            st.download_button("⬇️ Download Azure check results", data=csv_out, file_name=f"azure_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv", mime='text/csv')
            # Clear PAT from session state to avoid keeping it in memory in the UI
            try:
                st.session_state['az_pat'] = ''
            except Exception:
                pass
    else:
        if 'Azure Link' not in df.columns:
            st.info("No `Azure Link` column found in data — add the Azure work item URL to the `Azure Link` column to enable cross-checking.")

    # Export options
    st.subheader("Export KPI Summary")
    csv_bytes = kpi_df.to_csv(index=False).encode('utf-8')
    st.download_button("Download KPI CSV", data=csv_bytes, file_name="kpi_summary.csv", mime="text/csv")
else:
    st.info("Upload the CSV file (use the provided template from this repo).")
