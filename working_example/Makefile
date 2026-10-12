HERE=$(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))
BASE=vscode_remote_trybal
DIST=$(HERE)/dist
BAL_BUILDER=$(BASE)/bal_builder.py

GRAALVM=~/.sdkman/candidates/java/25.0.1-graal/bin/java
REGVM=~/.sdkman/candidates/java/11.0.32-amzn/bin/java

JVM=$(REGVM)

build:
	-yes|/bin/rm -r $(DIST)
	python3					\
	$(BAL_BUILDER)				\
	build-graalvm main.bal			\
	--test					\
	--visualize				\
	-o  $(DIST)


jrun: #build
	$(JVM) -jar $(DIST)/main.jar --format markdown --output sample.md ./main.bal
