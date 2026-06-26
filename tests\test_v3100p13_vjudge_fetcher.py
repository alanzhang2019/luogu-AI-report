"""v3.10.0.13 · 测 fetch_vjudge_profile 内部 step=20→45→65→85→95→98→100 平滑推进."""
import vjudge_fetcher as vf

steps = []
def _cb(s, t, m):
    steps.append((s, t, m))
    print(f"  step={s}/{t}  {m}")

print("[1] 调用 fetch_vjudge_profile('MapMc') 测真实抓取")
try:
    raw = vf.fetch_vjudge_profile("MapMc", progress_cb=_cb)
    print(f"\n[2] 完成. total_ac={raw.get('total_ac')} solved_count={raw.get('solved_count')}")
    print(f"\n[3] 步骤时间线:")
    for s, t, m in steps:
        print(f"     {s:>3}/{t}  {m}")
    # 校验关键 step 推进
    ratios = [s / t for s, t, m in steps if t > 0]
    assert len(ratios) >= 3, f"步骤数过少: {len(steps)}"
    assert max(ratios) >= 0.95, f"未到 95%: max={max(ratios)}"
    # step 必须单调递增
    pure_steps = [s for s, t, m in steps if t > 0]
    for i in range(1, len(pure_steps)):
        assert pure_steps[i] >= pure_steps[i - 1], f"step 倒退: {pure_steps}"
    print(f"\n[PASS] 进度平滑推进, 步数={len(steps)}, 终值={pure_steps[-1]}/100 ✅")
except Exception as e:
    print(f"[FAIL] {e!r}")
    import traceback
    traceback.print_exc()
