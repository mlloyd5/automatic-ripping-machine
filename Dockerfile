###########################################################
# Sleeve labels: build ptouch (ptouch-rs, 0.14.1 or later for the PT-E310BT) from its release tag. The
# upstream .deb is built on Ubuntu 24.04 and needs glibc 2.38, which the Ubuntu 22.04 base below lacks,
# so it is built here on Debian bullseye (glibc 2.31) instead.
FROM rust:1-bullseye AS ptouch-build
ARG PTOUCH_RS_REPO=https://github.com/vowstar/ptouch-rs.git
ARG PTOUCH_RS_TAG=v0.14.1
RUN apt-get update && apt-get install -y --no-install-recommends libusb-1.0-0-dev pkg-config && \
    rm -rf /var/lib/apt/lists/* && \
    cargo install --git "${PTOUCH_RS_REPO}" --tag "${PTOUCH_RS_TAG}" --locked --root /opt/ptouch ptouch-cli

###########################################################
# setup default directories and configs
FROM automaticrippingmachine/arm-dependencies:1.8.0 AS base

LABEL org.opencontainers.image.source=https://github.com/automatic-ripping-machine/automatic-ripping-machine
LABEL org.opencontainers.image.license=MIT
LABEL org.opencontainers.image.description='Automatic Ripping Machine for fully automated Blu-ray, DVD and audio disc ripping.'

EXPOSE 8080

# Setup folders and fstab
RUN \
    mkdir -m 0777 -p /home/arm \
    /home/arm/config /mnt/dev/sr0 \
    /mnt/dev/sr1 /mnt/dev/sr2 /mnt/dev/sr3 /mnt/dev/sr4 \
    /mnt/dev/sr5 /mnt/dev/sr6 /mnt/dev/sr7 /mnt/dev/sr8 \
    /mnt/dev/sr9 /mnt/dev/sr10 /mnt/dev/sr11 /mnt/dev/sr12 \
    /mnt/dev/sr13 /mnt/dev/sr14 /mnt/dev/sr15 /mnt/dev/sr16 \
    /mnt/dev/sr17 /mnt/dev/sr18 /mnt/dev/sr19 /mnt/dev/sr20 \
    && \
    echo "/dev/sr0  /mnt/dev/sr0  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr1  /mnt/dev/sr1  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr2  /mnt/dev/sr2  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr3  /mnt/dev/sr3  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr4  /mnt/dev/sr4  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr5  /mnt/dev/sr5  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr6  /mnt/dev/sr6  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr7  /mnt/dev/sr7  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr8  /mnt/dev/sr8  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr9  /mnt/dev/sr9  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr10  /mnt/dev/sr10  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr11  /mnt/dev/sr11  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr12  /mnt/dev/sr12  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr13  /mnt/dev/sr13  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr14  /mnt/dev/sr14  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr15  /mnt/dev/sr15  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr16  /mnt/dev/sr16  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr17  /mnt/dev/sr17  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr18  /mnt/dev/sr18  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr19  /mnt/dev/sr19  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab && \
    echo "/dev/sr20  /mnt/dev/sr20  udf,iso9660  defaults,users,utf8,ro  0  0" >> /etc/fstab


# Remove SSH
RUN rm -rf /etc/service/sshd /etc/my_init.d/00_regen_ssh_host_keys.sh

# Disc camera: OpenCV for the webcam daemon, evdev for the USB barcode scanner (built from source,
# the base image has gcc and python3-dev), tesseract for reading disc labels. mkvtoolnix so that
# mkvmerge/mkvinfo are at hand in the ripper for joining multi-disc rips and inspecting MKVs.
COPY ./requirements-camera.txt /tmp/requirements-camera.txt
RUN apt-get update && \
    apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng mkvtoolnix && \
    rm -rf /var/lib/apt/lists/* && \
    pip3 install --no-cache-dir --prefer-binary -r /tmp/requirements-camera.txt && \
    rm /tmp/requirements-camera.txt

# Sleeve labels: Pillow and qrcode draw the label, ptouch (built above) sends it to a Brother P-touch over
# USB; the udev rule gives the arm user the printer and keeps usblp off it.
COPY ./requirements-labels.txt /tmp/requirements-labels.txt
COPY ./setup/99-arm-ptouch.rules /etc/udev/rules.d/99-arm-ptouch.rules
COPY --from=ptouch-build /opt/ptouch/bin/ptouch /usr/local/bin/ptouch
RUN pip3 install --no-cache-dir --prefer-binary -r /tmp/requirements-labels.txt && \
    rm /tmp/requirements-labels.txt && \
    apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core libusb-1.0-0 && \
    rm -rf /var/lib/apt/lists/* && \
    ptouch --version

# Add ARMui service
RUN mkdir /etc/service/armui
COPY ./scripts/docker/runsv/armui.sh /etc/service/armui/run
RUN chmod +x /etc/service/armui/run

# Add disc camera / barcode scanner service (idles when CAMERA_ENABLED and BARCODE_ENABLED are false)
RUN mkdir /etc/service/armcamera
COPY ./scripts/docker/runsv/armcamera.sh /etc/service/armcamera/run
RUN chmod +x /etc/service/armcamera/run

# Create our startup scripts
RUN mkdir -p /etc/my_init.d
COPY ./scripts/docker/runit/arm_user_files_setup.sh /etc/my_init.d/arm_user_files_setup.sh
COPY ./scripts/docker/runit/start_udev.sh /etc/my_init.d/start_udev.sh
RUN chmod +x /etc/my_init.d/*.sh

# We need to use a modified udev
COPY ./scripts/docker/custom_udev /etc/init.d/udev
RUN chmod +x /etc/my_init.d/*.sh


###########################################################
# Final image pushed for use
FROM base AS automatic-ripping-machine

# Copy over source code
COPY . /opt/arm/

# Our docker udev rule
RUN ln -sv /opt/arm/setup/51-docker-arm.rules /lib/udev/rules.d/

# Allow git to be managed from the /opt/arm folders
RUN git config --global --add safe.directory /opt/arm

CMD ["/sbin/my_init"]
WORKDIR /home/arm
