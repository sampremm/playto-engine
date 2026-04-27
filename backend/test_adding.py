import django
from django.db import models

class MockState:
    def __init__(self):
        self.adding = True

class MockModel:
    def __init__(self):
        self._state = MockState()

m = MockModel()
print("Initial adding:", m._state.adding)
