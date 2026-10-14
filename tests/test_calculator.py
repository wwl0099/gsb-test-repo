"""Tests for Calculator - some pass, some fail intentionally."""

import pytest
from src.calculator import Calculator


@pytest.fixture
def calc():
    return Calculator()


def test_add(calc):
    assert calc.add(2, 3) == 5
    assert calc.add(-1, 1) == 0


def test_subtract(calc):
    assert calc.subtract(10, 5) == 5
    assert calc.subtract(0, 5) == -5


def test_multiply(calc):
    assert calc.multiply(3, 4) == 12
    assert calc.multiply(0, 100) == 0


def test_divide(calc):
    assert calc.divide(10, 2) == 5
    assert calc.divide(7, 2) == 3.5


def test_divide_by_zero(calc):
    with pytest.raises(ValueError, match="Cannot divide by zero"):
        calc.divide(10, 0)


def test_power(calc):
    assert calc.power(2, 3) == 8
    assert calc.power(5, 0) == 1


def test_average(calc):
    assert calc.average([1, 2, 3]) == 2
    assert calc.average([]) == 0


def test_average_single(calc):
    assert calc.average([42]) == 42
