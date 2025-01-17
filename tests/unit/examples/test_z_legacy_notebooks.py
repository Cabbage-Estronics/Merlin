#
# Copyright (c) 2021, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import contextlib
import itertools
import json
import os
import signal
import subprocess
import sys
import time
from distutils.spawn import find_executable
from os.path import dirname, realpath

import pytest

pytest.importorskip("cudf")
import cudf  # noqa: E402

import nvtabular.tools.data_gen as datagen  # noqa: E402
from tests.conftest import get_cuda_cluster  # noqa: E402

TEST_PATH = dirname(dirname(dirname(realpath(__file__))))

triton = pytest.importorskip("nvtabular.inference.triton")
data_conversions = pytest.importorskip("nvtabular.inference.triton.data_conversions")
ensemble = pytest.importorskip("nvtabular.inference.triton.ensemble")

grpcclient = pytest.importorskip("tritonclient.grpc")
tritonclient = pytest.importorskip("tritonclient")

TRITON_SERVER_PATH = find_executable("tritonserver")



@contextlib.contextmanager
def run_triton_server(modelpath):
    cmdline = [
        TRITON_SERVER_PATH,
        "--model-repository",
        modelpath,
        "--backend-config=tensorflow,version=2",
    ]
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = "0"
    with subprocess.Popen(cmdline, env=env) as process:
        try:
            with grpcclient.InferenceServerClient("localhost:8001") as client:
                # wait until server is ready
                for _ in range(60):
                    if process.poll() is not None:
                        retcode = process.returncode
                        raise RuntimeError(f"Tritonserver failed to start (ret={retcode})")

                    try:
                        ready = client.is_server_ready()
                    except tritonclient.utils.InferenceServerException:
                        ready = False

                    if ready:
                        yield client
                        return

                    time.sleep(1)

                raise RuntimeError("Timed out waiting for tritonserver to become ready")
        finally:
            # signal triton to shutdown
            process.send_signal(signal.SIGINT)



# pylint: disable=unused-import,broad-except

def test_movielens_example(tmpdir):
    _get_random_movielens_data(tmpdir, 10000, dataset="movie")
    _get_random_movielens_data(tmpdir, 10000, dataset="ratings")
    _get_random_movielens_data(tmpdir, 5000, dataset="ratings", valid=True)

    triton_model_path = os.path.join(tmpdir, "models")
    os.environ["INPUT_DATA_DIR"] = str(tmpdir)
    os.environ["MODEL_PATH"] = triton_model_path

    notebook_path = os.path.join(
        dirname(TEST_PATH),
        "examples/legacy/getting-started-movielens/",
        "02-ETL-with-NVTabular.ipynb",
    )
    _run_notebook(tmpdir, notebook_path)

    def _modify_tf_nb(line):
        return line.replace(
            # don't require graphviz/pydot
            "tf.keras.utils.plot_model(model)",
            "# tf.keras.utils.plot_model(model)",
        )

    def _modify_tf_triton(line):
        # models are already preloaded
        line = line.replace("triton_client.load_model", "# triton_client.load_model")
        line = line.replace("triton_client.unload_model", "# triton_client.unload_model")
        return line

    notebooks = []
    try:
        import torch  # noqa

        notebooks.append("03-Training-with-PyTorch.ipynb")
    except Exception:
        pass
    try:
        import nvtabular.inference.triton  # noqa
        import nvtabular.loader.tensorflow  # noqa

        notebooks.append("03-Training-with-TF.ipynb")
        has_tf = True

    except Exception:
        has_tf = False

    for notebook in notebooks:
        notebook_path = os.path.join(
            dirname(TEST_PATH),
            "examples/legacy/getting-started-movielens/",
            notebook,
        )
        if notebook == "03-Training-with-TF.ipynb":
            _run_notebook(tmpdir, notebook_path, transform=_modify_tf_nb)
        else:
            _run_notebook(tmpdir, notebook_path)

    # test out the TF inference movielens notebook if appropriate
    if has_tf and TRITON_SERVER_PATH:
        notebook = "04-Triton-Inference-with-TF.ipynb"
        notebook_path = os.path.join(
            dirname(TEST_PATH),
            "examples/legacy/getting-started-movielens/",
            notebook,
        )
        with run_triton_server(triton_model_path):
            _run_notebook(tmpdir, notebook_path, transform=_modify_tf_triton)


def test_multigpu_dask_example(tmpdir):
    with get_cuda_cluster() as cuda_cluster:
        os.environ["BASE_DIR"] = str(tmpdir)
        scheduler_port = cuda_cluster.scheduler_address

        def _nb_modify(line):
            # Use cuda_cluster "fixture" port rather than allowing notebook
            # to deploy a LocalCUDACluster within the subprocess
            line = line.replace("cluster = None", f"cluster = '{scheduler_port}'")
            # Use a much smaller "toy" dataset
            line = line.replace("write_count = 25", "write_count = 4")
            line = line.replace('freq = "1s"', 'freq = "1h"')
            # Use smaller partitions for smaller dataset
            line = line.replace("part_mem_fraction=0.1", "part_size=1_000_000")
            line = line.replace("out_files_per_proc=8", "out_files_per_proc=1")
            return line

        notebook_path = os.path.join(
            dirname(TEST_PATH), "examples/legacy/multi-gpu-toy-example/", "multi-gpu_dask.ipynb"
        )
        _run_notebook(tmpdir, notebook_path, _nb_modify)


def _run_notebook(tmpdir, notebook_path, transform=None):
    # read in the notebook as JSON, and extract a python script from it
    notebook = json.load(open(notebook_path, encoding="utf-8"))
    source_cells = [cell["source"] for cell in notebook["cells"] if cell["cell_type"] == "code"]
    lines = [
        transform(line.rstrip()) if transform else line
        for line in itertools.chain(*source_cells)
        if not (line.startswith("%") or line.startswith("!"))
    ]

    # save the script to a file, and run with the current python executable
    # we're doing this in a subprocess to avoid some issues using 'exec'
    # that were causing a segfault with globals of the exec'ed function going
    # out of scope
    script_path = os.path.join(tmpdir, "notebook.py")
    with open(script_path, "w") as script:
        script.write("\n".join(lines))
    subprocess.check_output([sys.executable, script_path])

def _get_random_movielens_data(tmpdir, rows, dataset="movie", valid=None):
    if dataset == "movie":
        json_sample_movie = {
            "conts": {},
            "cats": {
                "genres": {
                    "dtype": None,
                    "cardinality": 50,
                    "min_entry_size": 1,
                    "max_entry_size": 5,
                    "multi_min": 2,
                    "multi_max": 4,
                    "multi_avg": 3,
                },
                "movieId": {
                    "dtype": None,
                    "cardinality": 500,
                    "min_entry_size": 1,
                    "max_entry_size": 5,
                },
            },
        }
        cols = datagen._get_cols_from_schema(json_sample_movie)
    if dataset == "ratings":
        json_sample_ratings = {
            "conts": {},
            "cats": {
                "movieId": {
                    "dtype": None,
                    "cardinality": 500,
                    "min_entry_size": 1,
                    "max_entry_size": 5,
                },
                "userId": {
                    "dtype": None,
                    "cardinality": 500,
                    "min_entry_size": 1,
                    "max_entry_size": 5,
                },
            },
            "labels": {"rating": {"dtype": None, "cardinality": 5}},
        }
        cols = datagen._get_cols_from_schema(json_sample_ratings)

    df_gen = datagen.DatasetGen(datagen.UniformDistro(), gpu_frac=0.1)
    target_path = tmpdir
    df_gen.full_df_create(rows, cols, output=target_path)

    if dataset == "movie":
        movies_converted = cudf.read_parquet(os.path.join(tmpdir, "dataset_0.parquet"))
        movies_converted = movies_converted.drop_duplicates(["movieId"], keep="first")
        movies_converted.to_parquet(os.path.join(tmpdir, "movies_converted.parquet"))

    elif dataset == "ratings" and not valid:
        os.rename(os.path.join(tmpdir, "dataset_0.parquet"), os.path.join(tmpdir, "train.parquet"))
    else:
        os.rename(os.path.join(tmpdir, "dataset_0.parquet"), os.path.join(tmpdir, "valid.parquet"))
