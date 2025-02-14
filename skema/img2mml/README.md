# img2mml

This directory contains the code for the img2mml service, which processes images
of equations and returns presentation MathML corresponding to those equations.

The model was developed by Gaurav Sharma and Clay Morrison, and this wrapper
service was developed by Deepsana Shahi and Adarsh Pyarelal.

The model itself is not checked into the repository, but you can get it from
here:
https://kraken.sista.arizona.edu/skema/img2mml/models/cnn_xfmer_OMML-90K_best_model_RPimage.pt.

Place the model file in the `trained_models` directory.

The curl command below should do the trick.

```
curl -L https://kraken.sista.arizona.edu/skema/img2mml/models/cnn_xfmer_OMML-90K_best_model_RPimage.pt > trained_models/cnn_xfmer_OMML-90K_best_model_RPimage.pt
```

Then, run the invocation below to launch the Dockerized service:

```
docker-compose up --build
```

To test the service without Docker (e.g., for development purposes), ensure
that you have installed the required packages (run `pip install -e .[img2mml]`
in the root of the repository).

Then, run the following command to launch the img2mml server program:

```
uvicorn img2mml:app --reload
```

An example test program is provided as well, which you can invoke with:
Make sure that you are in generate_mathml folder.

```
python img2mml_demo.py
```
