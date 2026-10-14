"""A simple calculator with some bugs for GSB automation testing."""


class Calculator:
    """Basic calculator with add, subtract, multiply, divide."""

    def add(self, a, b):
        return a + b

    def subtract(self, a, b):
        return a - b

    def multiply(self, a, b):
        return a * b

    def divide(self, a, b):
        if b == 0:
            raise ValueError("Cannot divide by zero")
        return a / b

    def power(self, base, exp):
        return base**exp

    def average(self, numbers):
        if not numbers:
            return 0
        total = 0
        for n in numbers:
            total = total + n
        return total / len(numbers)
