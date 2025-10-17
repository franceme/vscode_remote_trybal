FROM cruizba/ubuntu-dind
RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list
RUN apt-get update && yes|apt-get install -y curl zstd zip unzip

#The python packages are installed in the .sdkman folder, so we need to install sdkman first
# Trying to install sdkman with a partial folder structure fails
RUN curl -s "https://get.sdkman.io" | bash
SHELL ["/bin/bash", "-c"]
RUN bash -c "source /root/.sdkman/bin/sdkman-init.sh && sdk i sbt 1.5.4 && sdk i java 23-graalce"
RUN curl -s "https://raw.githubusercontent.com/franceme/staticpy/master/sdk.sh" >> sdk.sh && chmod 777 sdk.sh && ./sdk.sh all  && rm sdk.sh
ENV GRAALVM_HOME=/root/.sdkman/candidates/java/23-graalce

RUN curl -s "https://raw.githubusercontent.com/franceme/staticpy/master/py_nsudo.sh" >> py_nsudo.sh && chmod 777 py_nsudo.sh && ./py_nsudo.sh eight && rm py_nsudo.sh

#RUN rm /bin/sh && ln -s /bin/bash /bin/sh

RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list && sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list
RUN apt-get update && yes|apt-get install binutils curl zstd zip unzip xz-utils

RUN python3 -m pip install --upgrade xonsh[full]

RUN curl -sL https://dist.ballerina.io/downloads/2201.7.1/ballerina-2201.7.1-swan-lake-linux-x64.deb --output ballerina.deb
RUN ar x ballerina.deb
RUN zstd -d < control.tar.zst | xz > control.tar.xz
RUN zstd -d < data.tar.zst | xz > data.tar.xz
RUN ar -m -c -a sdsd /tmp/ballerina.deb debian-binary control.tar.xz data.tar.xz
RUN rm debian-binary control.tar.xz data.tar.xz control.tar.zst data.tar.zst ballerina.deb
RUN dpkg -i /tmp/ballerina.deb

#RUN chmod 777 /bin/*_mini

