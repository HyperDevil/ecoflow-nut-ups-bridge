# ecoflow-nut-ups-bridge
Python docker bridge to connect Ecoflow River 3 UPS to Nut

This bridge script identifies itself as the android app, grabs credentials and connects to Ecoflow's MQTT service.

It starts a Nut server and the bridge script communicates with it via the Dummy driver.

When deploying, remember to set the environment variables, look in the .env file which one you need.

The container listens on port 3494, and connect to this container as slave, this docker is the master.
