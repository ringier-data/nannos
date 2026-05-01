"""Tests for _WeightedSemaphore — verifies correct budget accounting, priority, and deadlock freedom."""

import asyncio

import pytest

from playground_backend.catalog.sync import _WeightedSemaphore


class TestWeightedSemaphoreBasics:
    @pytest.mark.asyncio
    async def test_acquire_release(self):
        sem = _WeightedSemaphore(100)
        held = await sem.acquire(30)
        assert held == 30
        assert sem.available == 70
        sem.release(30)
        assert sem.available == 100

    @pytest.mark.asyncio
    async def test_acquire_capped_at_capacity(self):
        """Requesting more than capacity is capped — single oversized job runs alone."""
        sem = _WeightedSemaphore(50)
        held = await sem.acquire(200)
        assert held == 50  # capped
        assert sem.available == 0
        sem.release(50)
        assert sem.available == 50

    @pytest.mark.asyncio
    async def test_acquire_minimum_one(self):
        sem = _WeightedSemaphore(100)
        held = await sem.acquire(0)
        assert held == 1
        sem.release(1)

    @pytest.mark.asyncio
    async def test_fifo_ordering(self):
        """Normal waiters are served in FIFO order."""
        sem = _WeightedSemaphore(10)
        await sem.acquire(10)  # exhaust budget

        order: list[int] = []

        async def waiter(idx: int, weight: int):
            held = await sem.acquire(weight)
            order.append(idx)
            sem.release(held)

        # Queue up two waiters
        t1 = asyncio.create_task(waiter(1, 5))
        t2 = asyncio.create_task(waiter(2, 3))
        await asyncio.sleep(0.01)  # let them queue

        # Release enough for waiter 1
        sem.release(10)
        await asyncio.gather(t1, t2)
        assert order == [1, 2]  # FIFO

    @pytest.mark.asyncio
    async def test_adjust_down(self):
        sem = _WeightedSemaphore(100)
        held = await sem.acquire(50)
        new_held = sem.adjust_down(held, 30)
        assert new_held == 30
        assert sem.available == 70  # released 20

    @pytest.mark.asyncio
    async def test_adjust_down_no_increase(self):
        """adjust_down never increases the hold."""
        sem = _WeightedSemaphore(100)
        held = await sem.acquire(20)
        new_held = sem.adjust_down(held, 80)
        assert new_held == 20  # unchanged
        assert sem.available == 80


class TestUpgradePriority:
    @pytest.mark.asyncio
    async def test_upgrade_has_priority_over_new_acquire(self):
        """When budget is freed, upgrade waiters are served before new-acquire waiters."""
        sem = _WeightedSemaphore(100)

        # Two tasks hold all budget
        held_a = await sem.acquire(50)
        held_b = await sem.acquire(50)
        assert sem.available == 0

        served_order: list[str] = []

        async def new_acquirer():
            """A new file trying to enter the pipeline (normal priority)."""
            held = await sem.acquire(20)
            served_order.append("new_acquire")
            sem.release(held)

        async def upgrader():
            """An existing file upgrading after extract (high priority)."""
            nonlocal held_a
            held_a = await sem.upgrade(held_a, 80)
            served_order.append("upgrade")
            sem.release(held_a)

        # Queue both: new acquirer first, then upgrader
        t_new = asyncio.create_task(new_acquirer())
        await asyncio.sleep(0.01)  # ensure new_acquire is queued first
        t_upgrade = asyncio.create_task(upgrader())
        await asyncio.sleep(0.01)  # ensure upgrade is queued

        # Release B's budget — should go to upgrader first despite new_acquire being queued earlier
        sem.release(held_b)
        await asyncio.gather(t_new, t_upgrade)

        assert served_order[0] == "upgrade", f"Upgrade should be served first, got: {served_order}"

    @pytest.mark.asyncio
    async def test_multiple_upgrades_block_new_acquires(self):
        """New acquires are starved while upgrade waiters exist — by design."""
        sem = _WeightedSemaphore(100)

        # 3 tasks hold budget
        holds = [await sem.acquire(30) for _ in range(3)]
        assert sem.available == 10

        new_acquired = False

        async def new_file():
            nonlocal new_acquired
            held = await sem.acquire(20)
            new_acquired = True
            sem.release(held)

        async def upgrader(idx: int, current: int, target: int):
            held = await sem.upgrade(current, target)
            await asyncio.sleep(0.02)
            sem.release(held)

        # Queue new file first
        t_new = asyncio.create_task(new_file())
        await asyncio.sleep(0.01)

        # Start upgraders (they release 30 each, then need more via high-prio queue)
        upgrade_tasks = [asyncio.create_task(upgrader(i, holds[i], 50)) for i in range(3)]
        await asyncio.gather(*upgrade_tasks)

        # New file should NOT have acquired during upgrade phase
        # (it waited until upgraders were done)
        await asyncio.wait_for(t_new, timeout=2.0)
        assert new_acquired  # eventually gets through

    @pytest.mark.asyncio
    async def test_upgrade_downgrade(self):
        """upgrade() with smaller weight acts as adjust_down."""
        sem = _WeightedSemaphore(100)
        held = await sem.acquire(50)
        new_held = await sem.upgrade(held, 20)
        assert new_held == 20
        assert sem.available == 80


class TestUpgradeDeadlockFreedom:
    @pytest.mark.asyncio
    async def test_no_deadlock_multiple_upgraders(self):
        """Multiple tasks upgrading simultaneously do NOT deadlock.

        Critical scenario: N tasks each hold floor=20 (budget=100 exhausted),
        all try to upgrade.  Because upgrade() releases first and re-acquires
        with high priority, freed units circulate among upgraders.
        """
        budget = 100
        floor = 20
        sem = _WeightedSemaphore(budget)
        num_tasks = 5

        holds = [await sem.acquire(floor) for _ in range(num_tasks)]
        assert sem.available == 0

        page_counts = [84, 40, 40, 60, 30]
        completed: list[int] = []

        async def simulate_file(idx: int, current_held: int, target: int):
            held = await sem.upgrade(current_held, target)
            await asyncio.sleep(0.01)
            completed.append(idx)
            sem.release(held)

        tasks = [asyncio.create_task(simulate_file(i, holds[i], page_counts[i])) for i in range(num_tasks)]

        done, pending = await asyncio.wait(tasks, timeout=5.0)
        assert len(pending) == 0, f"DEADLOCK: {len(pending)} tasks stuck"
        assert len(completed) == num_tasks
        assert sem.available == budget

    @pytest.mark.asyncio
    async def test_no_deadlock_worst_case_all_max(self):
        """Even when all tasks want the full capacity, no deadlock occurs."""
        budget = 100
        floor = 20
        sem = _WeightedSemaphore(budget)
        num_tasks = 5

        holds = [await sem.acquire(floor) for _ in range(num_tasks)]
        assert sem.available == 0

        completed: list[int] = []

        async def simulate_file(idx: int, current_held: int):
            held = await sem.upgrade(current_held, budget)
            await asyncio.sleep(0.01)
            completed.append(idx)
            sem.release(held)

        tasks = [asyncio.create_task(simulate_file(i, holds[i])) for i in range(num_tasks)]

        done, pending = await asyncio.wait(tasks, timeout=5.0)
        assert len(pending) == 0, f"DEADLOCK: {len(pending)} tasks stuck"
        assert len(completed) == num_tasks
        assert sem.available == budget

    @pytest.mark.asyncio
    async def test_new_files_blocked_during_upgrades(self):
        """New file acquires are blocked while upgrade waiters exist.

        This is the key property that prevents memory pile-up.
        """
        budget = 100
        floor = 20
        sem = _WeightedSemaphore(budget)

        holds = [await sem.acquire(floor) for _ in range(5)]
        assert sem.available == 0

        new_file_entered = False
        upgrades_done = 0

        async def new_file():
            nonlocal new_file_entered
            held = await sem.acquire(floor)
            new_file_entered = True
            sem.release(held)

        async def upgrader(idx: int, current_held: int, target: int):
            nonlocal upgrades_done
            held = await sem.upgrade(current_held, target)
            await asyncio.sleep(0.01)
            upgrades_done += 1
            sem.release(held)

        # Start new file acquire FIRST
        t_new = asyncio.create_task(new_file())
        await asyncio.sleep(0.01)

        # Then start upgraders
        upgrade_tasks = [asyncio.create_task(upgrader(i, holds[i], 40)) for i in range(5)]

        await asyncio.gather(*upgrade_tasks)
        assert upgrades_done == 5

        # Only NOW should the new file enter
        await asyncio.wait_for(t_new, timeout=2.0)
        assert new_file_entered

    @pytest.mark.asyncio
    async def test_budget_never_overcommitted(self):
        """Total held units never exceed capacity at any point."""
        budget = 100
        floor = 20
        sem = _WeightedSemaphore(budget)

        max_consumed = 0
        lock = asyncio.Lock()

        async def track_consumed():
            nonlocal max_consumed
            async with lock:
                consumed = budget - sem.available
                if consumed > max_consumed:
                    max_consumed = consumed

        page_counts = [80, 60, 50, 40, 30]
        num_tasks = len(page_counts)
        holds = [await sem.acquire(floor) for _ in range(num_tasks)]

        async def simulate_file(idx: int, current_held: int, target: int):
            await track_consumed()
            held = await sem.upgrade(current_held, target)
            await track_consumed()
            await asyncio.sleep(0.02)
            await track_consumed()
            sem.release(held)

        tasks = [asyncio.create_task(simulate_file(i, holds[i], page_counts[i])) for i in range(num_tasks)]

        done, pending = await asyncio.wait(tasks, timeout=5.0)
        assert len(pending) == 0
        assert max_consumed <= budget

    @pytest.mark.asyncio
    async def test_realistic_pipeline_simulation(self):
        """End-to-end simulation: acquire floor → extract → upgrade → process → release.

        With budget=100 and floor=20, verifies that:
        - At most budget-worth of slides are ever in flight
        - All files complete
        - No deadlock
        """
        budget = 100
        floor = 20
        sem = _WeightedSemaphore(budget)
        num_files = 12
        page_counts = [21, 21, 21, 21, 39, 44, 40, 22, 22, 22, 22, 15]

        completed: list[int] = []
        max_held_total = 0
        lock = asyncio.Lock()

        async def file_pipeline(idx: int):
            nonlocal max_held_total
            # Step 1: acquire floor (blocks if budget full)
            held = await sem.acquire(floor)

            async with lock:
                consumed = budget - sem.available
                if consumed > max_held_total:
                    max_held_total = consumed

            # Step 2: extract (takes time, unknown page count)
            await asyncio.sleep(0.005)
            page_count = page_counts[idx]

            # Step 3: upgrade to real page count (high-priority, may block)
            held = await sem.upgrade(held, page_count)

            async with lock:
                consumed = budget - sem.available
                if consumed > max_held_total:
                    max_held_total = consumed

            # Step 4: summary + render + upload
            await asyncio.sleep(0.01)
            completed.append(idx)

            # Step 5: release
            sem.release(held)

        tasks = [asyncio.create_task(file_pipeline(i)) for i in range(num_files)]
        done, pending = await asyncio.wait(tasks, timeout=10.0)
        assert len(pending) == 0, f"DEADLOCK: {len(pending)} tasks stuck"
        assert len(completed) == num_files
        assert max_held_total <= budget
        assert sem.available == budget


class TestSlotContextManager:
    @pytest.mark.asyncio
    async def test_slot_acquires_and_releases(self):
        sem = _WeightedSemaphore(100)
        async with sem.slot(30) as held:
            assert held == 30
            assert sem.available == 70
        assert sem.available == 100

    @pytest.mark.asyncio
    async def test_slot_releases_on_exception(self):
        sem = _WeightedSemaphore(100)
        with pytest.raises(RuntimeError):
            async with sem.slot(40):
                raise RuntimeError("boom")
        assert sem.available == 100
