ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.24
ARG DFU_PROGRAMMER_VERSION=v1.1.0
ARG DFU_PROGRAMMER_REVISION=f23d6dfd6671b46cbf1279a61291f6fa18d7559e
ARG LIBUSB_VERSION=1.0.30
ARG LIBUSB_APK_VERSION=1.0.30-r0
ARG LIBUSB_SHA512=b14241bc499cdf353bb7fe02cea9a754b011f40ef0d0376ff8921f129f888b514d481e54c6aa380be04f655e027aaf4d6d9eba142b15758dea2e32f64af7b0c2

FROM ${BUILD_FROM} AS libusb-source
ARG LIBUSB_VERSION
ARG LIBUSB_SHA512

# Alpine 3.24 packages this unmodified release. Preserve the complete,
# checksum-verified corresponding source with the dynamically linked library.
RUN apk add --no-cache ca-certificates curl \
    && curl --fail --location --proto '=https' --tlsv1.2 \
        "https://github.com/libusb/libusb/releases/download/v${LIBUSB_VERSION}/libusb-${LIBUSB_VERSION}.tar.bz2" \
        --output /tmp/libusb.tar.bz2 \
    && echo "${LIBUSB_SHA512}  /tmp/libusb.tar.bz2" | sha512sum -c - \
    && install -d -m 0755 /src/libusb \
    && tar -xjf /tmp/libusb.tar.bz2 --strip-components=1 -C /src/libusb \
    && test -f /src/libusb/COPYING

FROM ${BUILD_FROM} AS dfu-programmer-builder
ARG DFU_PROGRAMMER_VERSION
ARG DFU_PROGRAMMER_REVISION

# Build the exact upstream release for each add-on architecture. Keeping its
# complete corresponding source in the runtime image satisfies GPLv2 section 3.
RUN apk add --no-cache \
        autoconf \
        automake \
        build-base \
        ca-certificates \
        git \
        libtool \
        libusb-dev \
    && git clone --depth 1 --branch "${DFU_PROGRAMMER_VERSION}" \
        https://github.com/dfu-programmer/dfu-programmer.git /src/dfu-programmer \
    && test "$(git -C /src/dfu-programmer rev-parse HEAD)" = "${DFU_PROGRAMMER_REVISION}" \
    && test -z "$(git -C /src/dfu-programmer status --porcelain)" \
    && rm -rf /src/dfu-programmer/.git \
    && cd /src/dfu-programmer \
    && ./bootstrap.sh \
    && ./configure --prefix=/usr/local \
    && make -j"$(getconf _NPROCESSORS_ONLN)"

FROM ${BUILD_FROM}
ARG LIBUSB_APK_VERSION

RUN apk add --no-cache \
        "libusb=${LIBUSB_APK_VERSION}" \
        python3 \
    && install -d -m 0755 /usr/share/licenses/cul868-flasher \
    && apk query --from installed --format json --fields name,version "*" \
        | python3 -c 'import json, sys; sys.stdout.writelines("{name}\t{version}\n".format(**package) for package in sorted(json.load(sys.stdin), key=lambda package: package["name"]))' \
        > /usr/share/licenses/cul868-flasher/alpine-packages.tsv

COPY --from=dfu-programmer-builder /src/dfu-programmer/src/dfu-programmer /usr/local/bin/dfu-programmer
# dfu-programmer is a separate GPLv2 program. Ship its full source beside the
# executable instead of relying on a third-party source offer.
COPY --from=dfu-programmer-builder /src/dfu-programmer /usr/src/dfu-programmer
# libusb remains dynamically linked and replaceable; this is the matching
# upstream source for the pinned unmodified Alpine package version.
COPY --from=libusb-source /src/libusb /usr/src/libusb-1.0.30
COPY LICENSES/Apache-2.0.txt /usr/share/licenses/cul868-flasher/LICENSE
COPY LICENSES/LGPL-2.1-or-later.txt /usr/share/licenses/cul868-flasher/LGPL-2.1-or-later.txt
COPY LICENSES/COMPONENTS.md /usr/share/licenses/cul868-flasher/COMPONENTS.md
COPY app /app
COPY run.sh /run.sh

RUN chmod 0755 /usr/local/bin/dfu-programmer /run.sh \
    && /usr/local/bin/dfu-programmer --help >/dev/null \
    && PYTHONPATH=/ python3 -c 'import app.main'

CMD ["/run.sh"]
