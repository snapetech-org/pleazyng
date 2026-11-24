"""Read a complete GitHub Actions inventory and combine split Mac qualification evidence."""


def inventory(path, key, get, error=ValueError, *, page_size=100):
    url = path + ("&" if "?" in path else "?") + f"per_page={page_size}"
    result = []
    expected = None
    for page in range(1, 101):
        data = get(url if page == 1 else f"{url}&page={page}")
        rows = data.get(key)
        if not isinstance(rows, list):
            break
        count = data.get("total_count", len(rows))
        if expected is None:
            expected = count
        if count != expected:
            break
        result.extend(rows)
        if len(result) == expected:
            return result
        if len(rows) != page_size or len(result) > expected:
            break
    raise error(f"Incomplete GitHub {key} inventory; refusing recovery.")


FINISH_MAC = "Finish macOS notarization and DMG"


def mac_package_evidence(jobs):
    """Fold hosted finishing steps into the historical build-macos proof shape."""
    finish = [job for job in jobs if job.get("name") == FINISH_MAC]
    producers = [job for job in jobs if job.get("name") == "build-macos"]
    if len(finish) != 1 or len(producers) != 1 or finish[0].get("conclusion") == "skipped":
        return jobs
    original, final = producers[0], finish[0]
    if original.get("conclusion") != "success":
        return jobs
    merged = dict(original)
    merged.update(status=final.get("status"), conclusion=final.get("conclusion"))
    # Preserve runner and job identity of the signing job. Only the hosted steps
    # that actually ran can supersede a skipped packaging step on that runner.
    if final.get("conclusion") == "success":
        steps = {s.get("name"): s for s in original.get("steps", [])}
        for step in final.get("steps", []):
            if step.get("conclusion") == "success":
                steps[step.get("name")] = step
        if (steps.get("Restore signed app and Apple submission", {}).get("conclusion") == "success"
                and steps.get("Restore prior notarization submission checkpoint", {}).get("conclusion") == "success"
                and steps.get("Notarize Application", {}).get("conclusion") == "skipped"):
            # The verified submission ID and signed bytes survived an earlier attempt.
            steps["Notarize Application"] = dict(steps["Restore signed app and Apple submission"], name="Notarize Application")
        merged["steps"] = list(steps.values())
    return [merged if job is original else job for job in jobs]
