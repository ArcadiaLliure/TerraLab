"""
calibration.py

Contains utilities for empirical calibration between aggregated 
DVNL radiance values and ground truth SQM measurements.
"""

import numpy as np
try:
    import joblib
except ImportError:
    joblib = None

class SQMCalibrationModel:
    """
    Calibrates a robust regression model to map DVNL aggregated predictors 
    (with an epsilon offset) to SQM measurements.
    """
    
    def __init__(self, epsilon: float = 1e-3):
        self.epsilon = epsilon
        self.model = None # Initialized lazily or via load
        self.fitted = False
        
    def _get_huber(self):
        from sklearn.linear_model import HuberRegressor
        return HuberRegressor()
        
    def fit(self, aggregated_dvnl: np.ndarray, elevation_m: np.ndarray, target_sqm: np.ndarray):
        """
        Fits a Log-linear model mapping log(DVNL_agg) + Elevation -> SQM.
        
        Args:
            aggregated_dvnl (np.ndarray): The kernel-aggregated DVNL radiance.
            elevation_m (np.ndarray): The observer's elevation in meters.
            target_sqm (np.ndarray): Real SQM measurements to fit against.
        """
        # We model SQM = alpha + beta * log10(A + eps) + gamma * (z/1000)
        X1 = np.log10(aggregated_dvnl + self.epsilon)
        X2 = elevation_m / 1000.0
        X = np.column_stack([X1, X2])
        
        if self.model is None:
            self.model = self._get_huber()
        self.model.fit(X, target_sqm)
        self.fitted = True
        return self
        
    def predict(self, aggregated_dvnl: np.ndarray, elevation_m: np.ndarray) -> np.ndarray:
        """
        Predicts the SQM from new kernel-aggregated DVNL data.
        
        Args:
            aggregated_dvnl (np.ndarray): Aggregated radiance.
            elevation_m (np.ndarray): Elevation map in meters or a scalar.
            
        Returns:
            np.ndarray: Predicted Zenith SQM values.
        """
        if not self.fitted:
            raise ValueError("Model must be fitted before prediction.")
            
        X1 = np.log10(aggregated_dvnl + self.epsilon)
        
        # Ensure elevation has the right shape
        if np.isscalar(elevation_m):
            X2 = np.full_like(X1, elevation_m / 1000.0)
        else:
            X2 = elevation_m / 1000.0
            
        if self.model is None:
             raise ValueError("Model is not initialized. Load a model first or fit it.")
             
        X = np.column_stack([X1.ravel(), X2.ravel()])
        y_pred = self.model.predict(X)
        return y_pred.reshape(X1.shape)
        
    def save(self, filepath: str):
        """Saves the serialized model using joblib."""
        if joblib is None:
            raise ImportError("joblib is required to save the model.")
        joblib.dump({"model": self.model, "epsilon": self.epsilon}, filepath)
        
    def load(self, filepath: str):
        """Loads a serialized model from the given path."""
        if joblib is None:
            raise ImportError("joblib is required to load the model.")
        data = joblib.load(filepath)
        self.model = data["model"]
        self.epsilon = data["epsilon"]
        self.fitted = True
        return self
