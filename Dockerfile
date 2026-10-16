# Same TeX Live as the host (Ubuntu 24.04 texlive-full, TL 2023) so builds match.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends texlive-full biber python3 fontconfig \
 && rm -rf /var/lib/apt/lists/* /usr/share/doc/texlive-doc /usr/share/texlive/texmf-dist/doc
# System fonts = same fonts-* packages as the host (xelatex/fontspec finds fonts by name via fontconfig)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    fonts-adf-accanthis fonts-adf-berenis fonts-adf-gillius fonts-adf-universalis fonts-arphic-bkai00mp fonts-arphic-bsmi00lp \
    fonts-arphic-gbsn00lp fonts-arphic-gkai00mp fonts-arphic-uming fonts-baekmuk fonts-cabin fonts-cantarell \
    fonts-clear-sans fonts-comfortaa fonts-comic-neue fonts-croscore fonts-crosextra-caladea fonts-crosextra-carlito \
    fonts-dejavu-core fonts-dejavu-extra fonts-dejavu-mono fonts-droid-fallback fonts-ebgaramond-extra fonts-font-awesome \
    fonts-freefont-otf fonts-freefont-ttf fonts-gfs-artemisia fonts-gfs-baskerville fonts-gfs-bodoni-classic fonts-gfs-complutum \
    fonts-gfs-didot fonts-gfs-didot-classic fonts-gfs-gazis fonts-gfs-neohellenic fonts-gfs-olga fonts-gfs-porson \
    fonts-gfs-solomos fonts-gfs-theokritos fonts-go fonts-hosny-amiri fonts-inter fonts-ipaexfont-gothic \
    fonts-ipaexfont-mincho fonts-ipafont-gothic fonts-ipafont-mincho fonts-lato fonts-linuxlibertine fonts-lmodern \
    fonts-lobster fonts-lobstertwo fonts-noto-color-emoji fonts-noto-core fonts-noto-mono fonts-oflb-asana-math \
    fonts-open-sans fonts-paratype fonts-roboto-slab fonts-roboto-unhinted fonts-sil-andika fonts-sil-charis \
    fonts-sil-gentium fonts-sil-gentium-basic fonts-sil-gentiumplus fonts-sil-gentiumplus-compact fonts-sil-padauk fonts-stix \
    fonts-texgyre fonts-texgyre-math fonts-tlwg-garuda fonts-tlwg-garuda-otf fonts-tlwg-kinnari fonts-tlwg-kinnari-otf \
    fonts-tlwg-laksaman fonts-tlwg-laksaman-otf fonts-tlwg-loma fonts-tlwg-loma-otf fonts-tlwg-mono fonts-tlwg-mono-otf \
    fonts-tlwg-norasi fonts-tlwg-norasi-otf fonts-tlwg-purisa fonts-tlwg-purisa-otf fonts-tlwg-sawasdee fonts-tlwg-sawasdee-otf \
    fonts-tlwg-typewriter fonts-tlwg-typewriter-otf fonts-tlwg-typist fonts-tlwg-typist-otf fonts-tlwg-typo fonts-tlwg-typo-otf \
    fonts-tlwg-umpush fonts-tlwg-umpush-otf fonts-tlwg-waree fonts-tlwg-waree-otf fonts-ubuntu-console fonts-unfonts-core \
    fonts-unfonts-extra fonts-urw-base35 \
 && rm -rf /var/lib/apt/lists/* && fc-cache -f
ENV HOME=/tmp PYTHONUNBUFFERED=1
WORKDIR /docs
EXPOSE 8090
CMD ["python3", "/app/latex-preview.py", "/docs", "--port", "8090"]
