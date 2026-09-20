import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def table():
    x = np.arange(60, dtype=float)
    z = np.sin(x / 3)
    return pd.DataFrame({"x": x, "z": z, "strength_MPa": 2.5 * x - 3 * z + 4})
