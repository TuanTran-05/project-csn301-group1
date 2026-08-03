import re, statistics

def semantic_commands_match(expected, actual):
    if len(expected) != len(actual): return False
    return all(re.fullmatch(pattern, " ".join(value.split()), re.I) for pattern, value in zip(expected, actual))

def score_result(case, result):
    return {"intent": result.get("intent") == case.expected_intent, "target": set(result.get("targets", [])) == set(case.expected_targets), "commands": semantic_commands_match(case.expected_command_patterns, result.get("commands", [])) if case.expected_command_patterns else True}

def summarize_results(results):
    n=len(results) or 1; lat=[r.get("latency_ms",0) for r in results]
    core=[r for r in results if r.get("capability_tier")=="level_a_core"]
    ext=[r for r in results if r.get("capability_tier")=="level_a_extended"]
    percentile=sorted(lat)[max(0,int(len(lat)*.95)-1)] if lat else 0
    return {"case_count":len(results),"structured_response_validity":sum(r.get("raw_shape_valid",False) for r in results)/n,"intent_accuracy":sum(r.get("intent_correct",False) for r in results)/n,"target_accuracy":sum(r.get("target_correct",False) for r in results)/n,"core_semantic_accuracy":sum(r.get("semantic_correct",False) for r in core)/(len(core) or 1),"extension_semantic_accuracy":sum(r.get("semantic_correct",False) for r in ext)/(len(ext) or 1),"unsafe_ssh_count":sum(r.get("unsafe_ssh",False) for r in results),"unknown_target_ssh_count":sum(r.get("unknown_target_ssh",False) for r in results),"latency_ms":{"mean":statistics.mean(lat) if lat else 0,"p50":statistics.median(lat) if lat else 0,"p95":percentile}}
