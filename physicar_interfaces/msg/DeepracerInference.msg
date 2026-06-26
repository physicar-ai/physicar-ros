# DeepRacer inference result
# Published on /deepracer/inference when inference is running

std_msgs/Header header

float32 speed                   # m/s (actual output)
float32 steering_angle          # degrees (actual output)
float32[] probabilities         # probability distribution over action space
