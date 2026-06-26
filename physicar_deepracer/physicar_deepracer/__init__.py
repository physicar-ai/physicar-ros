"""PhysiCar DeepRacer — Python helpers.

Only the model-conversion path (ModelLoader: TensorFlow .pb -> TFLite) lives here;
inference itself runs in the C++ node. The C++ model_loader shells out to
`python3 -c "from physicar_deepracer.model_loader import ModelLoader"`.
"""
