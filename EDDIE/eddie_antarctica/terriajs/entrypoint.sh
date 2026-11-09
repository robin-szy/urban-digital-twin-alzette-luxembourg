#!/bin/bash

# Used by docker to import environment variables into a docker container without baking them into the image.
# Used when runtime environment variables are not enough, for example, when a config file needs to be written.

# In a cloud environment there isn't a port value, this is derived from the use of https (port 443) so there's no need to include the port.
BACKEND_URL=$BACKEND_HOST
if [[ ! -z "$BACKEND_PORT" ]]; then
  BACKEND_URL=$BACKEND_URL:$BACKEND_PORT
fi

export BACKEND_URL

GEOSERVER_URL=$GEOSERVER_HOST
if [[ ! -z "$GEOSERVER_PORT" ]]; then
  GEOSERVER_URL=$GEOSERVER_URL:$GEOSERVER_PORT
fi

export GEOSERVER_URL

# List of variables to substitute
ENV_VARS_TO_FILL='$CESIUM_ACCESS_TOKEN,$LINZ_BASEMAPS_API_KEY,$BACKEND_URL,$GEOSERVER_URL'

# Substitute variables and save whole file to variable, sponge is not available to buffer.
for FILE_TO_SUB in \
  "wwwroot/config.json" \
  "wwwroot/init/catalog.json" \
  "wwwroot/date-selector.js" \
  "wwwroot/water-level-control.js"
do
  SUBBED=$(envsubst "$ENV_VARS_TO_FILL" < "$FILE_TO_SUB")
  # Overwrite original file.
  echo "$SUBBED" > "$FILE_TO_SUB"
done

# Inject the Digital Twin date selector into Terria's page.
if ! grep -q "date-selector.js" wwwroot/index.html
then
  sed -i \
    's#</body>#<script src="/date-selector.js?v=2"></script></body>#' \
    wwwroot/index.html
fi
# Inject the 3D water-level control into Terria's page.
if ! grep -q "water-level-control.js" wwwroot/index.html
then
  sed -i \
    's#</body>#<script src="/water-level-control.js?v=21"></script></body>#' \
    wwwroot/index.html
fi
# Add only the optional flood-cost popup adapter; existing page scripts stay intact.
if [[ -f "wwwroot/flood-cost-popup.js" ]]; then
  SUBBED=$(envsubst "$ENV_VARS_TO_FILL" < "wwwroot/flood-cost-popup.js")
  echo "$SUBBED" > "wwwroot/flood-cost-popup.js"
  if ! grep -q 'src="/flood-cost-popup.js' wwwroot/index.html; then
    sed -i 's#</body>#<script src="/flood-cost-popup.js?v=1"></script></body>#' wwwroot/index.html
  fi
fi
# Run original terria startup command
node ./node_modules/terriajs-server/lib/app.js --config-file serverconfig.json


