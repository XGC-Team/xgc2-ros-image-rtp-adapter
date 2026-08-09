cmake_minimum_required(VERSION 3.0.2)
project(ros_image_rtp_adapter)

find_package(catkin REQUIRED COMPONENTS rospy sensor_msgs)

catkin_package()

set(
  ROS_IMAGE_RTP_COMMON_DIR
  "${CMAKE_CURRENT_SOURCE_DIR}/../ros_image_rtp_adapter"
  CACHE PATH "Directory containing the shared ros_image_rtp_adapter Python package"
)
if(NOT EXISTS "${ROS_IMAGE_RTP_COMMON_DIR}/runtime.py")
  message(FATAL_ERROR "ROS_IMAGE_RTP_COMMON_DIR does not contain runtime.py")
endif()

catkin_install_python(
  PROGRAMS scripts/image_rtp_adapter scripts/publish_test_jpeg
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

# The encoder, frame validation, control socket, settings, and lifecycle are
# shared verbatim with ROS 2. Only this tiny rospy executable is ROS-version
# specific.
install(
  DIRECTORY ${ROS_IMAGE_RTP_COMMON_DIR}/
  DESTINATION ${CATKIN_GLOBAL_PYTHON_DESTINATION}/ros_image_rtp_adapter
  FILES_MATCHING PATTERN "*.py"
)

install(
  DIRECTORY config launch
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
)
