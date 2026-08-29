"""Always Free 的 ARM 额度按账号类型分，而且被 Oracle 砍过半。

## 故障

用户开了两台 2 OCPU / 12 GB / 100 GB 的 A1，过几天**被销毁一台，硬盘还在**。

面板当时把免费额度写死成 `FREE_A1_OCPU = 4.0` / `FREE_A1_MEMORY_GB = 24.0`，
于是两台合计 4 / 24 正好"顶满"，校验放行。但 Oracle 现行文档写的是：

    "All tenancies get the first 1,500 OCPU hours and 9,000 GB hours per month
     for free for VM instances using the VM.Standard.A1.Flex shape...
     For Always Free tenancies, this is equivalent to 2 OCPUs and 12 GB of memory."
    "you can create one or two OCI Ampere A1 Compute instances, 2 OCPUs total"

免费号的上限是 2 / 12 —— 只装得下那两台里的一台。这个算术是唯一能精确解释
「不是两台都掉、也不是都留、恰好剩下合规的一台」的东西。

**关于 Oracle 具体做了什么，本文件刻意不下结论**：这次下调的生效日和整改期限
只见于第三方报道（InfoQ / heise / Linuxiac），Oracle 从未公告；官方对超额 A1 的
唯一表述是「现有实例被禁用、30 天后删除」，不是「终止超出那台并保留引导卷」；
另有闲置回收（7 天内 CPU/网络/内存 95 分位均 <20%，内存仅 A1）同样能解释
「两台只掉一台」。见 test_the_block_message_explains_the_change_without_over_claiming。
（「硬盘还在」本身有据：OCI 终止实例时**默认保留**引导卷。）

## 两件必须分开的事

  * 额度**多大**：只看账号类型。不能计费的 Always Free 租户 2 / 12，
    升级过的（PAYG）仍是 4 / 24 —— 那句 "For **Always Free** tenancies" 是
    限定语，1,500 OCPU 小时对可计费账号是免费额度而不是天花板。
  * 额度是否**硬拦**：看 free_only 开关 + 账号类型（hard_free_caps）。

第一版修复把两者并成了一个判断，结果「付费账号打开仅免费模式」被压到 2 / 12，
凭空少一半。下面 test_a_paid_account_keeps_the_larger_allowance_even_in_free_only_mode
就是钉这个的。
"""

from __future__ import annotations

import pathlib
import re

from app import free_quota
from app.free_quota import FREE_A1_MEMORY_GB, FREE_A1_OCPU, a1_caps

_EMPTY = {
    "usage": {"a1_ocpu": 0.0, "a1_memory_gb": 0.0, "e2_micro_count": 0, "block_storage_gb": 0.0}
}


def _guard(ocpus, memory, *, tier="free", free_only=True, count=1, usage=None, boot=100):
    return free_quota.validate_launch_against_quota(
        shape="VM.Standard.A1.Flex",
        ocpus=ocpus,
        memory_in_gbs=memory,
        boot_volume_size_in_gbs=boot,
        boot_volume_vpus_per_gb=10,
        free_only_mode=free_only,
        account_tier=tier,
        usage=usage or _EMPTY,
        count=count,
    )


# ------------------------------------------------------------------ 额度本身


def test_always_free_arm_is_two_ocpu_twelve_gb():
    assert a1_caps("free") == (2.0, 12.0)
    assert (FREE_A1_OCPU, FREE_A1_MEMORY_GB) == (2.0, 12.0)


def test_an_upgraded_account_still_gets_four_and_twentyfour():
    assert a1_caps("paid") == (4.0, 24.0)


def test_an_unknown_tier_is_treated_as_free():
    """空串 / unknown 一律按免费处理 —— 一个拼错的或从备份导进来的 tier
    不该悄悄把上限放宽到付费档，那正是这次故障的形状。"""
    for tier in ("", "unknown", "  ", "FREE"):
        assert a1_caps(tier) == (2.0, 12.0), tier


def test_a_paid_account_keeps_the_larger_allowance_even_in_free_only_mode():
    """额度**多大**只看账号类型；free_only 只决定**是否硬拦**。

    并成一个判断的话，付费用户打开「仅使用免费额度」会被压到 2 / 12 ——
    而 4 / 24 本来就是他的免费额度。
    """
    assert a1_caps("paid") == (4.0, 24.0)
    # free_only 打开 → 硬拦，但拦的是 4 / 24 这条线，不是 2 / 12。
    assert free_quota.hard_free_caps(True, "paid") is True
    assert _guard(4, 24, tier="paid", free_only=True).ok is True
    assert _guard(5, 30, tier="paid", free_only=True).ok is False


# ------------------------------------------------------------------ 用户那个场景


def test_the_reported_scenario_two_2c12g_boxes_is_blocked():
    """用户报的就是这个：两台 2C12G = 4 / 24 = 免费额度的**两倍**。"""
    g = _guard(2, 12, count=2)
    assert g.ok is False
    assert any(i.code == "a1_over_free_cap" for i in g.issues), g.issues


def test_one_2c12g_box_exactly_fills_the_free_allowance():
    """一台就是上限本身，必须放行 —— 修过头把正常用法也拦掉同样是 bug。"""
    assert _guard(2, 12).ok is True


def test_two_boxes_fit_only_when_each_takes_half():
    """想要两台机器就得对半分：1 OCPU / 6 GB 各一台。"""
    assert _guard(1, 6, count=2).ok is True
    assert _guard(1, 6, count=3).ok is False


def test_the_block_message_explains_the_change_without_over_claiming():
    """撞上这条的人多半是照着旧攻略开 4C24G，得告诉他额度变了。

    但**措辞必须止于文档写了的东西**：
      * 生效日期和整改期限只见于第三方报道（InfoQ / heise / Linuxiac），
        Oracle 从未公告 —— 把它们写成官方口径就是又一次替 Oracle 下结论。
      * 官方对超额 A1 的唯一正面表述是「现有实例被禁用，30 天后删除」，
        **不是**「终止超出的那一台并保留引导卷」。而且「两台掉一台」同样可以由
        闲置回收（7 天内 CPU/网络/内存 95 分位均 <20%）解释。

    这一整轮反复修的就是「面板替 Oracle 断言它没说过的话」，别在修复文案里
    再犯一次。
    """
    msg = " ".join(i.message for i in _guard(4, 24).issues)
    # 该说的：现行上限、比旧额度少一半、没有公告、后果不确定、付费不受限。
    assert "2 OCPU / 12 GB" in msg
    assert "4 / 24" in msg
    assert "没有公告" in msg
    assert "可能被停用或回收" in msg
    assert "付费账号" in msg
    # 不该说的：编出来的日期，和 Oracle 从没承诺过的确切动作。
    for over_claim in ("2026-06-15", "2026-08-18", "自动终止", "引导卷保留"):
        assert over_claim not in msg, f"过度断言：{over_claim}"


# ------------------------------------------------------------------ 一键预设


def test_presets_follow_the_account_tier():
    """预设也得按账号类型走 —— 一张静态表必然对其中一边是错的。

    写 4C24G:免费号照着点会开出超一倍的配置然后被收走一台；
    写 2C12G:升级号被无端砍掉一半，而那本来就是他的额度。
    """
    from app.oci_client import launch_quick_presets

    for tier in ("free", "paid"):
        cap_cpu, cap_mem = a1_caps(tier)
        a1 = [x for x in launch_quick_presets(tier) if free_quota.is_a1_shape(x["shape"])]
        assert a1, tier
        # 至少有一个「用满额度」的档，也至少有一个「对半分、可开两台」的档。
        assert any(x["ocpus"] == cap_cpu and x["memory_in_gbs"] == cap_mem for x in a1), tier
        assert any(x["ocpus"] * 2 <= cap_cpu for x in a1), tier
        for x in a1:
            assert x["ocpus"] <= cap_cpu and x["memory_in_gbs"] <= cap_mem, (tier, x["label"])


def test_the_meta_cache_is_keyed_on_the_account_tier():
    """预设按 tier 生成之后，tier 就必须进缓存键。

    否则一个刚升级完的租户会继续拿到缓存里那份免费号的预设 —— 界面少给他一半额度，
    而且没有任何东西会让它失效。
    """
    import inspect

    from web.backend.launch_service import meta_cache_key

    assert "account_tier" in inspect.getsource(meta_cache_key)


def test_every_free_preset_fits_inside_the_free_allowance():
    """预设是「一键」入口，它推荐什么，用户就开什么。

    原来这里是「免费 ARM 4C24G」—— 面板在一键推荐一个整整超一倍、且必被 Oracle
    回收的配置。这是本次故障里最该被挡住的一环。
    """
    from app.oci_client import LAUNCH_QUICK_PRESETS

    cap_cpu, cap_mem = a1_caps("free")
    seen_a1 = False
    for preset in LAUNCH_QUICK_PRESETS:
        if not free_quota.is_a1_shape(preset["shape"]):
            continue
        seen_a1 = True
        assert preset["ocpus"] <= cap_cpu, preset["label"]
        assert preset["memory_in_gbs"] <= cap_mem, preset["label"]
        assert _guard(preset["ocpus"], preset["memory_in_gbs"],
                      boot=preset["boot_volume_size_in_gbs"]).ok is True, preset["label"]
    assert seen_a1, "一个 A1 预设都没有了？"


def test_free_presets_do_not_default_to_a_billable_performance_tier():
    """标着「免费」的预设不该默认选一个自己都在警告的档位。

    BOOT_VPU_PRESETS 把 >20 标成「可能额外计费」，free_quota 也会为 vpu>10 挂告警，
    而这三个预设原来全写着 120 —— 自相矛盾。想要更高性能仍可在向导里手动选。
    """
    from app.oci_client import LAUNCH_QUICK_PRESETS

    for preset in LAUNCH_QUICK_PRESETS:
        assert preset["boot_volume_vpus_per_gb"] <= 10, preset["label"]


# ------------------------------------------------------------------ 防复发


def test_no_guard_path_hardcodes_the_old_numbers():
    """这次故障的本质是「把 Oracle 的数字抄进代码」。

    额度只能有一个来源（a1_caps）。守卫代码里再出现裸的 4.0 / 24.0 就是又埋了一次
    同样的雷 —— Oracle 下次调整时没有任何东西会失败。
    """
    src = pathlib.Path("app/free_quota.py").read_text(encoding="utf-8")
    # 只看代码，注释里当然会提到这两个数字（解释这次改动本身）。
    code = "\n".join(
        ln.split("#", 1)[0] for ln in src.splitlines() if not ln.strip().startswith("#")
    )
    body = code.split("def a1_caps", 1)[1]  # 常量定义之后的部分
    for bad in (r"\b4\.0\b", r"\b24\.0\b"):
        hits = [m for m in re.finditer(bad, body)]
        # PAID_* 常量在 a1_caps 之前定义，这里不该再出现字面量。
        assert not hits, f"{bad} 又被写死进守卫逻辑了：{body[max(0, hits[0].start()-90):hits[0].end()+40]}"


def test_the_mirror_constant_agrees_with_the_free_caps():
    """oci_client.ALWAYS_FREE_LIMITS 是一份展示用的镜像，走散了会让界面说谎。"""
    from app.oci_client import ALWAYS_FREE_LIMITS

    assert ALWAYS_FREE_LIMITS["a1_ocpu"] == FREE_A1_OCPU
    assert ALWAYS_FREE_LIMITS["a1_memory_gb"] == FREE_A1_MEMORY_GB
