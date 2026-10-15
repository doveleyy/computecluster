"""SVM grid search, sized to whatever CPU quota the job was given.

Demonstrates running a genuinely expensive search deliberately slowly. The
container is hard-capped by `--cpus`, so this consumes only the share it was
allocated no matter how long it runs — useful when you want a laptop to stay
cool and quiet rather than finish quickly.

The key line is `n_jobs`: read it from the quota rather than using -1. Inside a
container `-1` sees the *host's* cores, so it would fan out far wider than the
quota allows and spend the difference on context switching.
"""

import json
import os
import time
from pathlib import Path

import joblib
import pandas as pd
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import GridSearchCV, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

dataset_path = Path(os.environ["HOME_PLATFORM_DATASET"])
output_directory = Path(os.environ["HOME_PLATFORM_OUTPUT_DIR"])
cpu_limit = float(os.environ.get("HOME_PLATFORM_CPU_LIMIT", "1"))
workers = max(1, int(cpu_limit))

frame = pd.read_csv(dataset_path)
features = frame.drop(columns=["target"])
target = frame["target"]
x_train, x_test, y_train, y_test = train_test_split(
    features, target, test_size=0.25, random_state=42, stratify=target
)

pipeline = Pipeline([("scale", StandardScaler()), ("svm", SVC())])
grid = {
    "svm__C": [0.1, 1, 10, 100],
    "svm__gamma": ["scale", 0.01, 0.1, 1],
    "svm__kernel": ["rbf", "poly"],
}

print(f"cpu quota={cpu_limit} -> n_jobs={workers}")
print(f"fitting {4 * 4 * 2} candidates x 5 folds = 160 fits")

started = time.time()
search = GridSearchCV(pipeline, grid, cv=5, n_jobs=workers, verbose=0)
search.fit(x_train, y_train)
elapsed = time.time() - started

accuracy = accuracy_score(y_test, search.predict(x_test))
print(f"best params : {search.best_params_}")
print(f"cv accuracy : {search.best_score_:.4f}")
print(f"test accuracy: {accuracy:.4f}")
print(f"elapsed     : {elapsed:.1f}s at {cpu_limit} CPU")

joblib.dump(search.best_estimator_, output_directory / "model.joblib")
(output_directory / "metrics.json").write_text(
    json.dumps(
        {
            "cv_accuracy": search.best_score_,
            "test_accuracy": accuracy,
            "best_params": {k: str(v) for k, v in search.best_params_.items()},
            "elapsed_seconds": round(elapsed, 1),
            "cpu_limit": cpu_limit,
            "n_jobs": workers,
        },
        indent=2,
    )
    + "\n"
)
(output_directory / "report.txt").write_text(
    classification_report(y_test, search.predict(x_test))
)
print("wrote model.joblib, metrics.json, report.txt")
