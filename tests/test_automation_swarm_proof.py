import scripts.automation_swarm_proof as automation_swarm_proof


def test_automation_swarm_proof_compares_parallel_and_serial_runs() -> None:
    proof = automation_swarm_proof.run_proof()

    assert proof["result"] == "passed"
    assert proof["swarm"]["projects"] == 2
    assert proof["swarm"]["completed_projects"] == 2
    assert proof["swarm"]["handoff_count"] == 12
    assert proof["comparison"]["safety_preserved"] is True


def test_automation_swarm_proof_fails_closed_without_handoffs(monkeypatch) -> None:
    monkeypatch.setattr(
        automation_swarm_proof,
        "_real_fixture_run",
        lambda *_args, **_kwargs: ({}, {}, {"started_count": 0}),
    )

    proof = automation_swarm_proof.run_proof()

    assert proof["result"] == "failed"
    assert proof["comparison"]["safety_preserved"] is False
