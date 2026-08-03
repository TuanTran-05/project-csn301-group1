"""Human-guided rollback suggestions; this module never executes inverses."""

def build_rollback_guidance(assessment, commands):
    family = assessment.operation_families[0] if assessment.operation_families else None
    if family == "ipv4_acl":
        return ["Restore the saved ACL and interface stanzas; do not auto-apply an inverse."]
    if family == "ios_dhcp_pool":
        return ["Compare the pre-change backup DHCP section before considering no ip dhcp pool."]
    if family == "single_area_ospf":
        return ["Restore the saved router ospf section; do not infer prior process state."]
    if assessment.capability_tier == "best_effort":
        return ["Apply rollback_commands manually only after reviewing the pre-change backup."]
    return ["Review rollback_commands and the pre-change backup before manual recovery."]
