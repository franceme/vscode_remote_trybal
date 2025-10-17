# https://ballerina.io/learn/cli-commands/
# https://ballerina.io/learn/scan-tool/
HERE=$(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))
SRC=$(HERE)/
# https://ballerina.io/learn/deployment-guidelines-overview/

# https://ballerina.io/learn/build-the-executable-locally/
build:
	cd $(SRC)/ && bal build

run: build
	cd $(SRC)/ && bal run

# https://ballerina.io/learn/containerized-deployment/#execute-the-docker-image
# https://ballerina.io/learn/code-to-cloud-deployment/
build_docker: build
	cd $(SRC)/ && bal build --cloud=docker

# https://ballerina.io/learn/build-the-executable-locally/
build_graalvm: build
	cd $(SRC)/ && bal build --graalvm

