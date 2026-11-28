import sys
from types import FrameType
from typing import Any

from irimi._atomic_total import AtomicTotal

ROUNDS_UNTIL_EVERY_PYTHON_TRACES_EACH_STEP = 2
ADDS_IN_A_ROUND = 3


def test_an_amount_added_while_another_is_being_added_is_not_lost() -> None:
    total = AtomicTotal()
    amounts_added_inside = 0

    def add_at_every_step(frame: FrameType, event: str, argument: object) -> Any:
        nonlocal amounts_added_inside
        if frame.f_code is not AtomicTotal.add.__code__:
            return None
        frame.f_trace_opcodes = True
        if event == "opcode":
            total.add(1)
            amounts_added_inside += 1
        return add_at_every_step

    for _ in range(ROUNDS_UNTIL_EVERY_PYTHON_TRACES_EACH_STEP):
        sys.settrace(add_at_every_step)
        try:
            for _ in range(ADDS_IN_A_ROUND):
                total.add(1)
        finally:
            sys.settrace(None)

    amounts_added_outside = ROUNDS_UNTIL_EVERY_PYTHON_TRACES_EACH_STEP * ADDS_IN_A_ROUND
    assert amounts_added_inside > 0
    assert total.read() == amounts_added_inside + amounts_added_outside
