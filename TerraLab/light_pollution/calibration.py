"""
calibration.py

Conté utilitats per a la calibració empírica entre valors de radiància
DVNL agregats i mesures SQM de referència.
"""

import numpy as np

try:
    import joblib
except ImportError:
    joblib = None


class SQMCalibrationModel:
    """
    Calibra un model de regressió robust per mapar predictors DVNL agregats
    (amb desplaçament epsilon) a mesures SQM.
    """

    def __init__(self, epsilon: float = 1e-3):
        self.epsilon = epsilon
        self.model = None  # Inicialitzat sota demanda o amb load
        self.fitted = False

    def _get_huber(self):
        from sklearn.linear_model import HuberRegressor

        return HuberRegressor()

    def fit(
        self,
        aggregated_dvnl: np.ndarray,
        elevation_m: np.ndarray,
        target_sqm: np.ndarray,
    ):
        """
        Ajusta un model log-lineal que relaciona
        log(DVNL_agg) + elevació -> SQM.

        Paràmetres:
        - aggregated_dvnl (np.ndarray): Radiància DVNL agregada pel nucli.
        - elevation_m (np.ndarray): Elevació de l'observador en metres.
        - target_sqm (np.ndarray): Mesures SQM reals per a l'ajust.

        Retorna:
        - SQMCalibrationModel: Instància ajustada del model.
        """
        # Model utilitzat:
        # SQM = alpha + beta * log10(A + eps) + gamma * (z / 1000)
        X1 = np.log10(aggregated_dvnl + self.epsilon)
        X2 = elevation_m / 1000.0
        X = np.column_stack([X1, X2])

        if self.model is None:
            self.model = self._get_huber()
        self.model.fit(X, target_sqm)
        self.fitted = True
        return self

    def predict(
        self, aggregated_dvnl: np.ndarray, elevation_m: np.ndarray
    ) -> np.ndarray:
        """
        Prediu l'SQM a partir de noves dades DVNL agregades per nucli.

        Paràmetres:
        - aggregated_dvnl (np.ndarray): Radiància agregada.
        - elevation_m (np.ndarray): Mapa d'elevació en metres o escalar.

        Retorna:
        - np.ndarray: Valors SQM zenitals predits.
        """
        if not self.fitted:
            raise ValueError(
                "El model ha d'estar ajustat abans de fer prediccions."
            )

        X1 = np.log10(aggregated_dvnl + self.epsilon)

        # Assegura que l'elevació tingui la forma adequada
        if np.isscalar(elevation_m):
            X2 = np.full_like(X1, elevation_m / 1000.0)
        else:
            X2 = elevation_m / 1000.0

        if self.model is None:
            raise ValueError(
                "El model no està inicialitzat. Carrega'n un o ajusta'l."
            )

        X = np.column_stack([X1.ravel(), X2.ravel()])
        y_pred = self.model.predict(X)
        return y_pred.reshape(X1.shape)

    def save(self, filepath: str):
        """
        Desa el model serialitzat amb joblib.

        Paràmetres:
        - filepath (str): Ruta del fitxer de sortida.

        Retorna:
        - None.
        """
        if joblib is None:
            raise ImportError("Cal joblib per desar el model.")
        joblib.dump({"model": self.model, "epsilon": self.epsilon}, filepath)

    def load(self, filepath: str):
        """
        Carrega un model serialitzat des d'una ruta.

        Paràmetres:
        - filepath (str): Ruta del fitxer a carregar.

        Retorna:
        - SQMCalibrationModel: Instància del model carregat.
        """
        if joblib is None:
            raise ImportError("Cal joblib per carregar el model.")
        data = joblib.load(filepath)
        self.model = data["model"]
        self.epsilon = data["epsilon"]
        self.fitted = True
        return self
