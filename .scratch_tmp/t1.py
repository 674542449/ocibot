import sys
sys.path.insert(0,'.')
from web.backend.quota_guard import check_launch_quota
class R:
    def __init__(s,d): s.ok=True; s.data=d; s.message=""
class S:
    def __init__(s,raises=False,snap=None): s.calls=0; s.r=raises; s.s=snap
    def get_free_quota_usage(s, free_only_mode=True, **k):
        s.calls+=1
        if s.r: raise RuntimeError("429 TooManyRequests")
        return R(s.s)
s=S(raises=True)
g=check_launch_quota(s, account_tier='free', shape='VM.Standard.A1.Flex', ocpus=4, memory_in_gbs=24, boot_volume_size_in_gbs=180)
print("raising: calls",s.calls,"ok",g.ok,"errs",g.error_messages())
s2=S(snap={"read_incomplete":True,"usage":{},"remaining":{}})
g2=check_launch_quota(s2, account_tier='free', shape='VM.Standard.A1.Flex', ocpus=4, memory_in_gbs=24, boot_volume_size_in_gbs=180)
print("partial: calls",s2.calls,"ok",g2.ok,"errs",g2.error_messages())
from app import free_quota
print("paid500", free_quota.validate_block_volume_against_quota(current_size_gb=0,new_size_gb=500,free_only_mode=True,account_tier='paid',usage={'usage':{'block_storage_gb':100.0}}).ok)
print("free150empty", free_quota.validate_block_volume_against_quota(current_size_gb=0,new_size_gb=150,free_only_mode=True,account_tier='free',usage={}).ok)
