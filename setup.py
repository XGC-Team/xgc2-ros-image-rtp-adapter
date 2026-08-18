from glob import glob

from setuptools import find_packages, setup

package_name = "ros_image_rtp_adapter"

setup(
    name=package_name,
    version="0.4.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", ["launch/image_rtp_adapter.launch.py"]),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="XGC2",
    maintainer_email="apt@xgc2.local",
    description="ROS Image or CompressedImage to media-edge H264/RTP adapter",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "image_rtp_adapter = ros_image_rtp_adapter.node:main",
            "publish_test_jpeg = ros_image_rtp_adapter.publish_test_jpeg:main",
        ],
    },
)
