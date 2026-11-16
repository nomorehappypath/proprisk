# The fixed ids of PropRisk's one office and one owner. The baseline
# migration (alembic/versions/0001_proprisk_baseline.py) inserts exactly
# these two rows; app/api/deps.py reads them back by these same ids on
# every request, since there is no sign-in to tell them apart otherwise.
LOCAL_TENANT_ID = "00000000-0000-0000-0000-000000000001"
LOCAL_USER_ID = "00000000-0000-0000-0000-000000000002"
