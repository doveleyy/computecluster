"""Small end-to-end training job for the Home Platform batch runner."""

import json
import os
from pathlib import Path

import joblib
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split

dataset_path = Path(os.environ["HOME_PLATFORM_DATASET"])
output_directory = Path(os.environ["HOME_PLATFORM_OUTPUT_DIR"])

frame = pd.read_csv(dataset_path)
features = frame.drop(columns=["target"])
target = frame["target"]
x_train, x_test, y_train, y_test = train_test_split(
    features,
    target,
    test_size=0.25,
    random_state=42,
    stratify=target,
)
model = LogisticRegression(max_iter=500)
model.fit(x_train, y_train)
accuracy = accuracy_score(y_test, model.predict(x_test))

joblib.dump(model, output_directory / "model.joblib")
(output_directory / "metrics.json").write_text(
    json.dumps({"accuracy": accuracy, "test_rows": len(x_test)}, indent=2)
)
print(f"accuracy={accuracy:.3f}; test_rows={len(x_test)}")
