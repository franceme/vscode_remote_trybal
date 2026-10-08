# https://ballerina.io/learn/cli-commands/
# https://ballerina.io/learn/scan-tool/
HERE=$(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))
SRC=$(HERE)
# https://ballerina.io/learn/deployment-guidelines-overview/

.PHONY: build run test build_docker build_graalvm

# https://ballerina.io/learn/build-the-executable-locally/
build:
	cd $(SRC) && bal build

# bal run, bal test and the build_* targets below compile the package themselves; depending on build would compile it twice.
run:
	cd $(SRC) && bal run

# https://ballerina.io/learn/test-ballerina-code/code-coverage-and-reporting/
test:
	cd $(SRC) && bal test --test-report --code-coverage --coverage-format=xml

# https://ballerina.io/learn/containerized-deployment/#execute-the-docker-image
# https://ballerina.io/learn/code-to-cloud-deployment/
build_docker:
	cd $(SRC) && bal build --cloud=docker

# https://ballerina.io/learn/build-the-executable-locally/
build_graalvm:
	cd $(SRC) && bal build --graalvm

