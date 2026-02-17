{ pkgs ? import <nixpkgs> {} }:
let
  whisperPkg =
    if builtins.hasAttr "whisper-cpp" pkgs
    then pkgs."whisper-cpp"
    else pkgs.whisper;
in
pkgs.mkShell {
  buildInputs = [
    pkgs.python311
    pkgs.python311Packages.pip
    pkgs.python311Packages.virtualenv
    pkgs.ollama
    whisperPkg
    pkgs.ffmpeg
    pkgs.portaudio
    pkgs.libsndfile
  ];

  shellHook = ''
    export PIP_DISABLE_PIP_VERSION_CHECK=1
    echo "Dev shell ready."
    echo "Install Python deps once: pip install -r requirements.txt"
    echo "Whisper command check: command -v whisper-cli || command -v whisper"
    echo "Ollama check: command -v ollama"
  '';
}
