.PHONY: setup services demo verify clean

setup services demo verify clean:
	bash ./start_demo.sh --$@
