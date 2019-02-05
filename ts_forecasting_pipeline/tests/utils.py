from typing import Optional
from datetime import datetime, timedelta
import logging

import pandas as pd
import numpy as np
from statsmodels.api import OLS

from ts_forecasting_pipeline import speccing, modelling, transforming


logger = logging.getLogger(__name__)


def create_dummy_model_state(
    data_start: datetime,
    data_range_in_hours: int,
    outcome_feature_transformation: Optional[
        transforming.ReversibleTransformation
    ] = None,
) -> modelling.ModelState:
    """
    Create a dummy model. data increases linearly, regressor is constant (useless).
    Use two different ways to define Series specs to test them.
    """
    dt_range = pd.date_range(
        data_start, data_start + timedelta(hours=data_range_in_hours), freq="1H"
    )
    outcome_values = [0]
    regressor_values = [5]
    for i in range(1, len(dt_range)):
        outcome_values.append(outcome_values[i - 1] + 1)
        regressor_values.append(5)
    outcome_series = pd.Series(index=dt_range, data=outcome_values)
    regressor_series = pd.Series(index=dt_range, data=regressor_values)
    specs = modelling.ModelSpecs(
        outcome_var=speccing.ObjectSeriesSpecs(
            outcome_series,
            name="my_outcome",
            feature_transformation=outcome_feature_transformation,
        ),
        model=OLS,
        lags=[1, 2],
        frequency=timedelta(hours=1),
        horizon=timedelta(minutes=120),
        remodel_frequency=timedelta(hours=48),
        regressors=[regressor_series],
        start_of_training=data_start
        + timedelta(hours=2),  # leaving room for NaN in lags
        end_of_testing=data_start + timedelta(hours=int(data_range_in_hours / 3)),
    )
    return modelling.ModelState(
        modelling.create_fitted_model(specs, version="0.1", save=False), specs
    )


class MyAdditionTransformation(transforming.ReversibleTransformation):
    def transform_series(self, x: pd.Series):
        logger.debug("Adding %s to %s ..." % (self.params.addition, x))
        return x + self.params.addition

    def back_transform(self, y: np.array):
        logger.debug("Subtracting %s from %s ..." % (self.params.addition, y))
        return y - self.params.addition


class MyMultiplicationTransformation(transforming.ReversibleTransformation):
    def transform_series(self, x: pd.Series):
        return x * self.params.factor

    def back_transform(self, y: np.array):
        return y / self.params.factor
